from gateway.run import GatewayRunner


def test_format_session_info_shows_sanitized_codex_account(monkeypatch):
    from gateway import run as gateway_run
    from agent import model_metadata

    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda: "gpt-5.5")
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"model": {"provider": "openai-codex"}},
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openai-codex",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "secret-token",
            "codex_account_alias": "OpenAI - jeremiahkear@gmail.com",
        },
    )
    monkeypatch.setattr(model_metadata, "get_model_context_length", lambda *args, **kwargs: 272_000)

    runner = object.__new__(GatewayRunner)

    session_info = runner._format_session_info()

    assert "◆ Codex account: gmail" in session_info
    assert "jeremiahkear" not in session_info
    assert "@" not in session_info
    assert "secret-token" not in session_info


def test_format_session_info_omits_codex_account_for_non_codex_provider(monkeypatch):
    from gateway import run as gateway_run
    from agent import model_metadata

    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda: "claude-sonnet-4-6")
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"model": {"provider": "anthropic"}},
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "anthropic",
            "base_url": "https://api.anthropic.com",
            "api_key": "secret-token",
            "codex_account_alias": "gmail",
        },
    )
    monkeypatch.setattr(model_metadata, "get_model_context_length", lambda *args, **kwargs: 200_000)

    runner = object.__new__(GatewayRunner)

    session_info = runner._format_session_info()

    assert "Codex account" not in session_info
    assert "secret-token" not in session_info
