import json

import pytest

from tools import credential_csw_tool as tool


def _service_config(_service):
    return {
        "allowed_hosts": ["facebook.com"],
        "username_ref": "op://AI-Allowed/facebook.com/username",
        "password_ref": "op://AI-Allowed/facebook.com/password",
        "totp_ref": "op://AI-Allowed/facebook.com/one-time password",
    }


def test_credential_csw_browser_fill_redacts_secret_values(monkeypatch):
    calls = []

    monkeypatch.setattr(tool, "_resolve_cdp_endpoint", lambda: "ws://127.0.0.1:18830/devtools/browser/test")
    monkeypatch.setattr(tool, "_service_config", _service_config)
    monkeypatch.setattr(tool, "_resolve_secret", lambda ref: "SECRET_USERNAME" if ref.endswith("username") else "SECRET_PASSWORD")

    def fake_cdp(method, params=None, target_id=None, timeout=20.0):
        calls.append((method, params or {}, target_id))
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "targetId": "tab-1", "url": "https://www.facebook.com/login"}]}
        assert method == "Runtime.evaluate"
        if len(calls) == 2:
            return {"result": {"value": {"ok": True, "username_found": True, "password_found": True, "reason": "login_fields_ready"}}}
        return {"result": {"value": {"ok": True, "username_filled": True, "password_filled": True, "submitted": False}}}

    monkeypatch.setattr(tool, "_cdp_call", fake_cdp)

    raw = tool.credential_csw_browser_fill("facebook", "login", submit=False)
    payload = json.loads(raw)

    assert payload["success"] is True
    assert payload["details"]["username_filled"] is True
    assert "SECRET_USERNAME" not in raw
    assert "SECRET_PASSWORD" not in raw
    assert calls[0][0] == "Target.getTargets"
    assert calls[1][0] == "Runtime.evaluate"
    assert calls[2][0] == "Runtime.evaluate"


def test_credential_csw_browser_fill_clicks_passkey_alternate_before_resolving_secrets(monkeypatch):
    calls = []
    resolved = []

    monkeypatch.setattr(tool, "_resolve_cdp_endpoint", lambda: "ws://127.0.0.1:18830/devtools/browser/test")
    monkeypatch.setattr(tool, "_service_config", _service_config)
    monkeypatch.setattr(tool, "_resolve_secret", lambda ref: resolved.append(ref) or "SECRET")

    def fake_cdp(method, params=None, target_id=None, timeout=20.0):
        calls.append((method, params or {}, target_id))
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "targetId": "tab-1", "url": "https://www.facebook.com/login"}]}
        expression = (params or {}).get("expression", "")
        assert "SECRET" not in expression
        return {"result": {"value": {
            "ok": False,
            "passkey_detected": True,
            "alternate_clicked": True,
            "retry_required": True,
            "reason": "alternate_clicked_retry_required",
        }}}

    monkeypatch.setattr(tool, "_cdp_call", fake_cdp)

    raw = tool.credential_csw_browser_fill("facebook", "login", submit=False)
    payload = json.loads(raw)

    assert payload["success"] is False
    assert payload["details"]["alternate_clicked"] is True
    assert payload["details"]["passkey_detected"] is True
    assert payload["details"]["retry_required"] is True
    assert resolved == []


def test_credential_csw_browser_fill_resolves_secrets_after_alternate_reveals_fields(monkeypatch):
    calls = []
    resolved = []

    monkeypatch.setattr(tool, "_resolve_cdp_endpoint", lambda: "ws://127.0.0.1:18830/devtools/browser/test")
    monkeypatch.setattr(tool, "_service_config", _service_config)
    monkeypatch.setattr(tool, "_resolve_secret", lambda ref: resolved.append(ref) or ("SECRET_USERNAME" if ref.endswith("username") else "SECRET_PASSWORD"))

    def fake_cdp(method, params=None, target_id=None, timeout=20.0):
        calls.append((method, params or {}, target_id))
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "targetId": "tab-1", "url": "https://www.facebook.com/login"}]}
        if len(calls) == 2:
            expression = (params or {}).get("expression", "")
            assert "SECRET" not in expression
            return {"result": {"value": {
                "ok": True,
                "passkey_detected": True,
                "alternate_clicked": True,
                "username_found": True,
                "password_found": True,
                "reason": "login_fields_ready",
            }}}
        expression = (params or {}).get("expression", "")
        assert "SECRET_USERNAME" in expression
        assert "SECRET_PASSWORD" in expression
        return {"result": {"value": {"ok": True, "username_filled": True, "password_filled": True, "submitted": False}}}

    monkeypatch.setattr(tool, "_cdp_call", fake_cdp)

    raw = tool.credential_csw_browser_fill("facebook", "login", submit=False)
    payload = json.loads(raw)

    assert payload["success"] is True
    assert payload["details"]["alternate_clicked"] is True
    assert payload["details"]["username_filled"] is True
    assert len(resolved) == 2
    assert "SECRET_USERNAME" not in raw
    assert "SECRET_PASSWORD" not in raw


def test_credential_csw_browser_fill_blocks_forbidden_cdp_before_secret_resolution(monkeypatch):
    resolved = []
    monkeypatch.setattr(tool, "_resolve_cdp_endpoint", lambda: "ws://127.0.0.1:18800/devtools/browser/shared")
    monkeypatch.setattr(tool, "_resolve_secret", lambda ref: resolved.append(ref) or "SECRET")

    raw = tool.credential_csw_browser_fill("facebook", "login", submit=False)

    assert "error" in raw
    assert "forbidden" in raw
    assert resolved == []


def test_totp_from_otpauth_known_vector():
    # RFC 6238 SHA1 test seed, represented as otpauth base32, at t=59 => 94287082 for 8 digits.
    uri = "otpauth://totp/Test?secret=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ&digits=8&period=30&algorithm=SHA1"
    assert tool._totp_from_otpauth(uri, now=59) == "94287082"
