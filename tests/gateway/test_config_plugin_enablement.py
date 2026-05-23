from types import SimpleNamespace

from gateway.config import GatewayConfig, Platform, PlatformConfig, _apply_env_overrides


def test_plugin_auto_enable_respects_explicit_disabled_config(monkeypatch):
    """A platform plugin's dependency check must not override enabled: false."""
    from gateway import platform_registry
    import hermes_cli.plugins

    monkeypatch.setattr(hermes_cli.plugins, "discover_plugins", lambda: None)
    monkeypatch.setattr(
        platform_registry.platform_registry,
        "plugin_entries",
        lambda: [
            SimpleNamespace(
                name="discord",
                check_fn=lambda: True,
                env_enablement_fn=None,
            )
        ],
    )

    config = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=False,
                extra={"_enabled_explicit": True},
            )
        }
    )

    _apply_env_overrides(config)

    assert config.platforms[Platform.DISCORD].enabled is False


def test_plugin_auto_enable_still_enables_when_not_explicitly_disabled(monkeypatch):
    from gateway import platform_registry
    import hermes_cli.plugins

    monkeypatch.setattr(hermes_cli.plugins, "discover_plugins", lambda: None)
    monkeypatch.setattr(
        platform_registry.platform_registry,
        "plugin_entries",
        lambda: [
            SimpleNamespace(
                name="discord",
                check_fn=lambda: True,
                env_enablement_fn=None,
            )
        ],
    )

    config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=False, extra={})}
    )

    _apply_env_overrides(config)

    assert config.platforms[Platform.DISCORD].enabled is True
