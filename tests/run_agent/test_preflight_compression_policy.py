"""Focused regression tests for preflight compression policy.

These cover the Matrix gateway failure mode where a session compressed on one
turn but stayed close enough to the trigger threshold that the next preflight
would immediately compact again.  The preflight path must re-estimate after
compression and stay bounded even when repeated passes are needed.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.context_compressor import ContextCompressor
from agent.conversation_loop import _maybe_run_preflight_compression


def _messages(count: int) -> list[dict]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i}"}
        for i in range(count)
    ]


def _agent_stub(*, threshold_tokens: int = 1_000, context_length: int = 2_000):
    compressor = SimpleNamespace(
        protect_first_n=1,
        protect_last_n=2,
        threshold_tokens=threshold_tokens,
        context_length=context_length,
    )
    return SimpleNamespace(
        compression_enabled=True,
        context_compressor=compressor,
        tools=[{"type": "function", "function": {"name": "noop", "parameters": {"type": "object"}}}],
        model="matrix-preflight-test-model",
        _emit_status=MagicMock(),
        _compress_context=MagicMock(),
        _empty_content_retries=7,
        _thinking_prefill_retries=7,
        _last_content_with_tools={"stale": True},
        _last_content_tools_all_housekeeping=True,
        _mute_post_response=True,
    )


def test_preflight_reestimates_and_runs_second_pass_when_still_over_threshold():
    """A first compaction that remains near threshold gets exactly one more pass.

    The important regression check is the post-compression estimate between
    passes: without it, preflight either stops too early or keeps compacting on
    stale pre-compression pressure.
    """
    agent = _agent_stub(threshold_tokens=1_000)
    original_messages = _messages(8)
    once_compressed = _messages(6)
    twice_compressed = _messages(5)
    agent._compress_context.side_effect = [
        (once_compressed, "system-after-pass-1"),
        (twice_compressed, "system-after-pass-2"),
    ]

    with patch(
        "agent.conversation_loop.estimate_request_tokens_rough",
        side_effect=[1_250, 1_100, 900],
    ) as estimate:
        messages, system_prompt, conversation_history = _maybe_run_preflight_compression(
            agent,
            original_messages,
            "base system",
            "cached system",
            "task-123",
            conversation_history=list(original_messages),
        )

    assert messages == twice_compressed
    assert system_prompt == "system-after-pass-2"
    assert conversation_history is None
    assert agent._compress_context.call_count == 2
    assert estimate.call_count == 3
    assert [call.kwargs["approx_tokens"] for call in agent._compress_context.call_args_list] == [1_250, 1_100]
    assert agent._empty_content_retries == 0
    assert agent._thinking_prefill_retries == 0
    assert agent._last_content_with_tools is None
    assert agent._last_content_tools_all_housekeeping is False
    assert agent._mute_post_response is False


def test_preflight_repeated_trigger_is_bounded_to_three_passes():
    """Even if every post-compression estimate remains above threshold, stop at 3."""
    agent = _agent_stub(threshold_tokens=1_000)
    original_messages = _messages(10)
    agent._compress_context.side_effect = [
        (_messages(9), "system-after-pass-1"),
        (_messages(8), "system-after-pass-2"),
        (_messages(7), "system-after-pass-3"),
        (_messages(6), "system-after-pass-4"),
    ]

    with patch(
        "agent.conversation_loop.estimate_request_tokens_rough",
        side_effect=[1_300, 1_250, 1_200, 1_150, 1_100],
    ) as estimate:
        messages, system_prompt, conversation_history = _maybe_run_preflight_compression(
            agent,
            original_messages,
            "base system",
            "cached system",
            "task-123",
            conversation_history=list(original_messages),
        )

    assert messages == _messages(7)
    assert system_prompt == "system-after-pass-3"
    assert conversation_history is None
    assert agent._compress_context.call_count == 3
    assert estimate.call_count == 4  # initial estimate + one verification after each pass


def test_observed_matrix_post_compression_estimate_stays_below_075_trigger():
    """The mitigation threshold keeps the observed Matrix post-compress size idle.

    Parent diagnosis observed ~130,357 post-compression tokens on a ~272k-token
    context.  At threshold=0.75 the trigger is 204,000, so global compression
    remains enabled but this size does not immediately compact again.
    """
    agent = _agent_stub(threshold_tokens=204_000, context_length=272_000)
    messages = _messages(8)

    with patch("agent.conversation_loop.estimate_request_tokens_rough", return_value=130_357):
        out_messages, out_prompt, out_history = _maybe_run_preflight_compression(
            agent,
            messages,
            "base system",
            "cached system",
            "task-123",
            conversation_history=list(messages),
        )

    assert agent.compression_enabled is True
    assert out_messages == messages
    assert out_prompt == "cached system"
    assert out_history == messages
    agent._compress_context.assert_not_called()
    agent._emit_status.assert_not_called()


def test_context_compressor_075_policy_derives_threshold_tail_and_protect_counts():
    """Config-derived policy values produce the Matrix mitigation budget."""
    with patch("agent.context_compressor.get_model_context_length", return_value=272_000):
        compressor = ContextCompressor(
            model="matrix-preflight-test-model",
            threshold_percent=0.75,
            protect_first_n=3,
            protect_last_n=20,
            summary_target_ratio=0.20,
            quiet_mode=True,
            abort_on_summary_failure=True,
        )

    assert compressor.threshold_percent == 0.75
    assert compressor.context_length == 272_000
    assert compressor.threshold_tokens == 204_000
    assert compressor.summary_target_ratio == 0.20
    assert compressor.tail_token_budget == 40_800
    assert compressor.protect_first_n == 3
    assert compressor.protect_last_n == 20
    assert compressor.abort_on_summary_failure is True
    assert compressor.should_compress(130_357) is False
    assert compressor.should_compress(204_000) is True
