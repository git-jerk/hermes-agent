from cron.delivery_policy import audit_chat_delivery_jobs, format_chat_delivery_violations


def test_audit_is_silent_for_local_only_jobs():
    violations = audit_chat_delivery_jobs([
        {"id": "local1", "name": "Local", "enabled": True, "state": "scheduled", "deliver": "local"},
        {"id": "none1", "name": "None", "enabled": True, "state": "scheduled", "deliver": None},
    ])

    assert violations == []
    assert format_chat_delivery_violations(violations) == ""


def test_audit_flags_active_origin_and_matrix_delivery_only():
    violations = audit_chat_delivery_jobs([
        {"id": "origin1", "name": "Origin", "enabled": True, "state": "scheduled", "deliver": "origin"},
        {"id": "matrix1", "name": "Matrix", "enabled": True, "state": "scheduled", "deliver": "local,matrix:!room"},
        {"id": "paused1", "name": "Paused", "enabled": True, "state": "paused", "deliver": "origin"},
        {"id": "disabled1", "name": "Disabled", "enabled": False, "state": "scheduled", "deliver": "matrix"},
    ])

    assert [item["id"] for item in violations] == ["origin1", "matrix1"]
    rendered = format_chat_delivery_violations(violations)
    assert "origin1" in rendered
    assert "matrix1" in rendered
    assert "Paused" not in rendered


def test_audit_honors_allowlist():
    violations = audit_chat_delivery_jobs([
        {"id": "allowed", "name": "Allowed report", "enabled": True, "state": "scheduled", "deliver": "origin"},
        {"id": "blocked", "name": "Blocked", "enabled": True, "state": "scheduled", "deliver": "matrix"},
    ], allowlist=["allowed"])

    assert [item["id"] for item in violations] == ["blocked"]
