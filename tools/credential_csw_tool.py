#!/usr/bin/env python3
"""Profile-scoped 1Password CSW browser credential injection tools.

These tools intentionally do not return credential material. They resolve only
profile-configured 1Password Connect Server Wrapper refs, inject into the
currently selected service login page over the profile's configured CDP lane,
and return status booleans/counts only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import struct
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_ALLOWED_SERVICES = {"facebook"}
_DEFAULT_REFS = {
    # Host defaults are safe routing metadata. Secret refs must be supplied by
    # the profile config so merely enabling this toolset in another profile
    # cannot resolve Seller's credentials by accident.
    "facebook": {
        "allowed_hosts": ["facebook.com", "www.facebook.com"],
    }
}
_FORBIDDEN_CDP_PORTS = {"18800", "18801"}


def _load_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _service_config(service: str) -> Dict[str, Any]:
    if service not in _ALLOWED_SERVICES:
        raise ValueError(f"unsupported service: {service}")
    cfg = _load_config()
    profile_cfg = (
        ((cfg.get("credential_csw") or {}).get("browser_login") or {}).get(service)
        or {}
    )
    merged = dict(_DEFAULT_REFS.get(service, {}))
    if isinstance(profile_cfg, dict):
        merged.update({k: v for k, v in profile_cfg.items() if v not in (None, "")})
    hosts = merged.get("allowed_hosts") or []
    if isinstance(hosts, str):
        hosts = [hosts]
    merged["allowed_hosts"] = [str(h).lower().lstrip(".") for h in hosts if str(h).strip()]
    return merged


def _resolve_secret(ref: str) -> str:
    if not ref.startswith("op://"):
        raise ValueError("credential ref must be op://")
    repo = Path("/Users/minimiah/.openclaw/workspace/LAWS_GUARDS")
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from lib.op_client import resolve_sync  # type: ignore[import-not-found]

    return resolve_sync(ref)


def _resolve_cdp_endpoint() -> str:
    try:
        from tools.browser_cdp_tool import _resolve_cdp_endpoint as _resolve

        return (_resolve() or "").strip()
    except Exception:
        return ""


def _canonical_port(endpoint: str) -> str:
    try:
        parsed = urllib.parse.urlparse(endpoint)
        return str(parsed.port or "")
    except Exception:
        m = re.search(r":(\d{2,5})(?:/|$)", endpoint)
        return m.group(1) if m else ""


def _endpoint_allowed(endpoint: str) -> bool:
    port = _canonical_port(endpoint)
    forbidden = set(_FORBIDDEN_CDP_PORTS)
    for raw in (os.environ.get("HERMES_FORBIDDEN_CDP_PORTS") or "").split(","):
        raw = raw.strip()
        if raw:
            forbidden.add(raw)
    return bool(endpoint) and port not in forbidden


def _cdp_call(method: str, params: Optional[Dict[str, Any]] = None, target_id: Optional[str] = None, timeout: float = 20.0) -> Dict[str, Any]:
    from tools.browser_cdp_tool import _cdp_call as _raw_cdp_call, _run_async

    endpoint = _resolve_cdp_endpoint()
    if not _endpoint_allowed(endpoint):
        raise RuntimeError("CDP endpoint is unavailable or forbidden for this coordinator lane")
    if not endpoint.startswith(("ws://", "wss://")):
        raise RuntimeError("resolved CDP endpoint is not a WebSocket URL")
    return _run_async(_raw_cdp_call(endpoint, method, params or {}, target_id, timeout))


def _host_allowed(url: str, allowed_hosts: list[str]) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return False
    host = host.lower()
    return any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts)


def _select_target(service_cfg: Dict[str, Any]) -> Dict[str, Any]:
    allowed_hosts = service_cfg.get("allowed_hosts") or []
    result = _cdp_call("Target.getTargets", {}, None, 20.0)
    targets = result.get("targetInfos") or []
    page_targets = [t for t in targets if t.get("type") == "page"]
    for target in page_targets:
        if _host_allowed(str(target.get("url") or ""), allowed_hosts):
            return target
    raise RuntimeError("no matching service tab found in this coordinator browser lane")


def _totp_from_otpauth(uri_or_secret: str, *, now: Optional[int] = None) -> str:
    value = uri_or_secret.strip()
    digits = 6
    period = 30
    algo = "SHA1"
    secret = value
    if value.lower().startswith("otpauth://"):
        parsed = urllib.parse.urlparse(value)
        qs = urllib.parse.parse_qs(parsed.query)
        secret = (qs.get("secret") or [""])[0]
        if qs.get("digits"):
            digits = int(qs["digits"][0])
        if qs.get("period"):
            period = int(qs["period"][0])
        if qs.get("algorithm"):
            algo = qs["algorithm"][0].upper()
    if not secret:
        raise ValueError("TOTP secret is empty")
    digest_name = {"SHA1": "sha1", "SHA256": "sha256", "SHA512": "sha512"}.get(algo, "sha1")
    key = base64.b32decode(secret.upper().replace(" ", ""), casefold=True)
    counter = int((now if now is not None else time.time()) // period)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, getattr(hashlib, digest_name)).digest()
    offset = digest[-1] & 0x0F
    code_int = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code_int % (10 ** digits)).zfill(digits)


def _js_literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _prepare_login_expression() -> str:
    return r"""
(async () => {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = el => !!el && !el.disabled && el.offsetParent !== null && el.getClientRects().length > 0;
  const bodyText = () => (document.body && document.body.innerText || '').toLowerCase();
  const challenge = ['captcha', 'cloudflare', 'security check', 'unusual activity'].find(x => bodyText().includes(x));
  if (challenge) return {ok:false, blocker:challenge, reason:'challenge_detected'};
  const fieldQuery = sels => sels.map(s => Array.from(document.querySelectorAll(s)).find(visible)).find(Boolean);
  const findFields = () => {
    const user = fieldQuery(['input[name="email"]','input#email','input[type="email"]','input[name="username"]','input[autocomplete="username"]','input[autocomplete="email"]','input[name="login"]','input[id*="email" i]']);
    const pass = fieldQuery(['input[name="pass"]','input#pass','input[type="password"]','input[autocomplete="current-password"]']);
    return {user, pass};
  };
  const passkeySeen = () => /passkey|security key|webauthn|touch id|face id/.test(bodyText());
  const ready = findFields();
  if (ready.user && ready.pass) return {ok:true, username_found:true, password_found:true, passkey_detected:passkeySeen(), alternate_clicked:false, reason:'login_fields_ready'};

  const phrases = [
    'try another way', 'use another way', 'choose another way', 'log in another way',
    'login another way', 'sign in another way', 'use password', 'use your password',
    'password instead', 'log in with password', 'login with password', 'sign in with password',
    'continue with password', 'use email', 'email or phone', 'use a different method',
    'other options', 'more options', 'not now'
  ];
  const labelFor = el => [el.getAttribute('aria-label'), el.getAttribute('title'), el.value, el.textContent]
    .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim().toLowerCase();
  const clickTargetFor = el => el.closest('button,a,[role="button"],input[type="button"],input[type="submit"]') || el;
  const candidates = Array.from(document.querySelectorAll('button,a,[role="button"],input[type="button"],input[type="submit"],div[tabindex],span[tabindex]'))
    .filter(visible)
    .map(el => ({el, label: labelFor(el)}))
    .filter(x => x.label);
  const alternate = candidates.find(x => {
    const label = x.label;
    const wantsPasskey = /passkey|security key|webauthn/.test(label) && !/password|another|different|not now/.test(label);
    if (wantsPasskey) return false;
    return phrases.some(p => label.includes(p));
  });
  let alternateClicked = false;
  let alternateLabel = '';
  if (alternate) {
    alternateClicked = true;
    alternateLabel = alternate.label.slice(0, 80);
    clickTargetFor(alternate.el).click();
    for (let i = 0; i < 12; i++) {
      await sleep(250);
      const fields = findFields();
      if (fields.user && fields.pass) return {ok:true, username_found:true, password_found:true, passkey_detected:passkeySeen(), alternate_clicked:true, alternate_label:alternateLabel, reason:'login_fields_ready'};
    }
  }
  const fields = findFields();
  return {
    ok:false,
    username_found:!!fields.user,
    password_found:!!fields.pass,
    passkey_detected:passkeySeen(),
    alternate_clicked:alternateClicked,
    alternate_label:alternateLabel || undefined,
    retry_required:alternateClicked,
    reason: alternateClicked ? 'alternate_clicked_retry_required' : (passkeySeen() ? 'passkey_no_password_alternate_found' : 'login_fields_not_found')
  };
})()
"""


def _login_expression(username: str, password: str, submit: bool) -> str:
    return f"""
(() => {{
  const text = (document.body && document.body.innerText || '').toLowerCase();
  const blocker = ['captcha', 'cloudflare', 'security check', 'unusual activity'].find(x => text.includes(x));
  if (blocker) return {{ok:false, blocker, reason:'challenge_detected'}};
  const visible = el => !!el && !el.disabled && el.offsetParent !== null;
  const setValue = (el, value) => {{
    el.focus();
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) desc.set.call(el, value); else el.value = value;
    el.dispatchEvent(new Event('input', {{bubbles:true}}));
    el.dispatchEvent(new Event('change', {{bubbles:true}}));
  }};
  const q = sels => sels.map(s => Array.from(document.querySelectorAll(s)).find(visible)).find(Boolean);
  const user = q(['input[name="email"]','input#email','input[type="email"]','input[name="username"]','input[autocomplete="username"]','input[autocomplete="email"]']);
  const pass = q(['input[name="pass"]','input#pass','input[type="password"]','input[autocomplete="current-password"]']);
  if (!user || !pass) return {{ok:false, username_found:!!user, password_found:!!pass, reason:'login_fields_not_found'}};
  setValue(user, {_js_literal(username)});
  setValue(pass, {_js_literal(password)});
  let submitted = false;
  if ({'true' if submit else 'false'}) {{
    const btn = q(['button[name="login"]','button[type="submit"]','input[type="submit"]']);
    if (btn) {{ btn.click(); submitted = true; }}
  }}
  return {{ok:true, username_filled:true, password_filled:true, submitted}};
}})()
"""


def _otp_expression(code: str, submit: bool) -> str:
    return f"""
(() => {{
  const visible = el => !!el && !el.disabled && el.offsetParent !== null;
  const setValue = (el, value) => {{
    el.focus();
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) desc.set.call(el, value); else el.value = value;
    el.dispatchEvent(new Event('input', {{bubbles:true}}));
    el.dispatchEvent(new Event('change', {{bubbles:true}}));
  }};
  const selectors = ['input[autocomplete="one-time-code"]','input[name*="code" i]','input[id*="code" i]','input[type="tel"]','input[type="text"]'];
  const field = selectors.map(s => Array.from(document.querySelectorAll(s)).find(visible)).find(Boolean);
  if (!field) return {{ok:false, otp_found:false, reason:'otp_field_not_found'}};
  setValue(field, {_js_literal(code)});
  let submitted = false;
  if ({'true' if submit else 'false'}) {{
    const btn = Array.from(document.querySelectorAll('button[type="submit"], button, input[type="submit"]')).find(visible);
    if (btn) {{ btn.click(); submitted = true; }}
  }}
  return {{ok:true, otp_filled:true, submitted}};
}})()
"""


def credential_csw_browser_fill(service: str = "facebook", mode: str = "login", submit: bool = False) -> str:
    """Resolve profile-scoped CSW refs and fill a browser login/OTP form."""
    service = (service or "facebook").strip().lower()
    mode = (mode or "login").strip().lower()
    if service not in _ALLOWED_SERVICES:
        return tool_error("unsupported service; allowed services: facebook")
    if mode not in {"login", "otp"}:
        return tool_error("mode must be 'login' or 'otp'")

    endpoint = _resolve_cdp_endpoint()
    if not _endpoint_allowed(endpoint):
        return tool_error("CDP endpoint is unavailable or forbidden for this coordinator lane")

    try:
        service_cfg = _service_config(service)
        target = _select_target(service_cfg)
        target_id = target.get("targetId")
        if not target_id:
            return tool_error("matching service tab did not expose a targetId")

        if mode == "login":
            prepare_result = _cdp_call(
                "Runtime.evaluate",
                {"expression": _prepare_login_expression(), "returnByValue": True, "awaitPromise": True},
                target_id,
                20.0,
            )
            prepare_payload = ((prepare_result.get("result") or {}).get("value") or {})
            if not isinstance(prepare_payload, dict):
                prepare_payload = {"ok": False, "reason": "unexpected_browser_result"}
            if not prepare_payload.get("ok"):
                safe = {
                    "success": False,
                    "service": service,
                    "mode": mode,
                    "target_url_host_ok": _host_allowed(str(target.get("url") or ""), service_cfg.get("allowed_hosts") or []),
                    "submitted": False,
                    "details": {k: v for k, v in prepare_payload.items() if k not in {"ok"}},
                }
                return json.dumps(safe, ensure_ascii=False)
            username_ref = str(service_cfg.get("username_ref") or "")
            password_ref = str(service_cfg.get("password_ref") or "")
            username = _resolve_secret(username_ref)
            password = _resolve_secret(password_ref)
            expression = _login_expression(username, password, bool(submit))
            prepare_details = {k: v for k, v in prepare_payload.items() if k not in {"ok", "username_found", "password_found", "reason"}}
        else:
            totp_ref = str(service_cfg.get("totp_ref") or "")
            raw = _resolve_secret(totp_ref)
            expression = _otp_expression(_totp_from_otpauth(raw), bool(submit))
            prepare_details = {}

        result = _cdp_call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            target_id,
            20.0,
        )
        payload = ((result.get("result") or {}).get("value") or {})
        if not isinstance(payload, dict):
            payload = {"ok": False, "reason": "unexpected_browser_result"}
        safe = {
            "success": bool(payload.get("ok")),
            "service": service,
            "mode": mode,
            "target_url_host_ok": _host_allowed(str(target.get("url") or ""), service_cfg.get("allowed_hosts") or []),
            "submitted": bool(payload.get("submitted")),
            "details": {**prepare_details, **{k: v for k, v in payload.items() if k not in {"ok"}}},
        }
        return json.dumps(safe, ensure_ascii=False)
    except Exception as exc:
        logger.debug("credential_csw_browser_fill failed: %s", exc)
        return tool_error(f"credential CSW browser fill failed: {type(exc).__name__}: {exc}")


CREDENTIAL_CSW_BROWSER_FILL_SCHEMA: Dict[str, Any] = {
    "name": "credential_csw_browser_fill",
    "description": (
        "Use the profile-approved 1Password CSW path to fill a login or OTP form "
        "in the coordinator's own dedicated browser lane. This tool never returns "
        "secret values; it returns only status booleans/counts. Currently scoped "
        "to Facebook for Seller. Use only after the user has approved login within "
        "the current workflow. If a site defaults to passkey, the tool first looks "
        "for an alternate password/TOTP method and does not resolve secrets until "
        "username/password fields are visible. Stop/report if CAPTCHA, Cloudflare, "
        "unusual-activity, or another unknown security challenge appears."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "enum": ["facebook"],
                "description": "Credential/service profile to use. Currently only 'facebook'.",
                "default": "facebook",
            },
            "mode": {
                "type": "string",
                "enum": ["login", "otp"],
                "description": "Fill username/password login fields, or a current TOTP/one-time-code field.",
                "default": "login",
            },
            "submit": {
                "type": "boolean",
                "description": "Whether to click the visible submit/login/continue button after filling. Defaults false so the agent can inspect first.",
                "default": False,
            },
        },
    },
}


def _credential_csw_check() -> bool:
    try:
        cfg = _service_config("facebook")
        if not cfg.get("username_ref") or not cfg.get("password_ref"):
            return False
        endpoint = _resolve_cdp_endpoint()
        return _endpoint_allowed(endpoint)
    except Exception:
        return False


registry.register(
    name="credential_csw_browser_fill",
    toolset="credential_csw",
    schema=CREDENTIAL_CSW_BROWSER_FILL_SCHEMA,
    handler=lambda args, **kw: credential_csw_browser_fill(
        service=args.get("service", "facebook"),
        mode=args.get("mode", "login"),
        submit=bool(args.get("submit", False)),
    ),
    check_fn=_credential_csw_check,
    emoji="🔐",
)
