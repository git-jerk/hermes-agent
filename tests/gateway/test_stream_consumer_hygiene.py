from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


class _Adapter:
    MAX_MESSAGE_LENGTH = 4096
    message_len_fn = None

    def __init__(self):
        self.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="msg1"))
        self.edit_message = AsyncMock(return_value=SimpleNamespace(success=True))

    def truncate_message(self, text, limit, len_fn=len):
        return [text]


@pytest.mark.asyncio
async def test_context_summary_prefix_is_not_delivered():
    adapter = _Adapter()
    consumer = GatewayStreamConsumer(
        adapter,
        "chat1",
        StreamConsumerConfig(edit_interval=0.01, buffer_threshold=5, cursor=""),
    )

    consumer.on_delta("[CONTEXT COMPACTION — REFERENCE ONLY] old handoff")
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.02)
    consumer.finish()
    await task

    adapter.send.assert_not_awaited()
    adapter.edit_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_scratchpad_commentary_is_suppressed_but_normal_commentary_sends():
    adapter = _Adapter()
    consumer = GatewayStreamConsumer(
        adapter,
        "chat1",
        StreamConsumerConfig(edit_interval=0.01, buffer_threshold=5, cursor=""),
    )

    consumer.on_commentary("Need open orders search.")
    consumer.on_commentary("I’ll check the current state and report back.")
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.02)
    consumer.finish()
    await task

    adapter.send.assert_awaited_once()
    sent = adapter.send.call_args.kwargs.get("content") or adapter.send.call_args.args[1]
    assert sent == "I’ll check the current state and report back."
