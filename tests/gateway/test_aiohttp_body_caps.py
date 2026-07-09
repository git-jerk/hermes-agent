"""Regression tests: aiohttp servers must set an explicit ``client_max_size``.

Without it, aiohttp falls back to its implicit 1 MiB default and — worse —
handlers that only check ``Content-Length`` can be bypassed entirely by
chunked transfer-encoding requests (#58536 webhook, #58902 raft lineage).
These tests pin the wiring for the three servers fixed in this follow-up:
bluebubbles, teams, and the ``hermes proxy`` server.
"""

import inspect


def test_bluebubbles_app_sets_client_max_size():
    import gateway.platforms.bluebubbles as bb

    assert bb._WEBHOOK_MAX_BODY_BYTES > 0
    src = inspect.getsource(bb.BlueBubblesAdapter.connect)
    assert "client_max_size=_WEBHOOK_MAX_BODY_BYTES" in src


def test_teams_app_sets_client_max_size():
    import plugins.platforms.teams.adapter as teams

    assert teams._MAX_BODY_BYTES > 0
    src = inspect.getsource(teams.TeamsAdapter.connect)
    assert "client_max_size=_MAX_BODY_BYTES" in src


def test_proxy_app_sets_client_max_size():
    import hermes_cli.proxy.server as proxy_server

    # Local divergence (codex-proxy 413 fix): the proxy's cap is
    # PROXY_MAX_BODY_BYTES (100 MiB default, HERMES_PROXY_MAX_BODY_BYTES
    # env override), not api_server's MAX_REQUEST_BYTES — conversation
    # payloads with base64 images routinely exceed 10 MB and the upstream
    # enforces its own real limit. Upstream's invariant still holds: the
    # cap exists, is explicit, and bounds chunked bodies.
    assert proxy_server.PROXY_MAX_BODY_BYTES >= 1_048_576
    src = inspect.getsource(proxy_server.create_app)
    assert "client_max_size=PROXY_MAX_BODY_BYTES" in src
