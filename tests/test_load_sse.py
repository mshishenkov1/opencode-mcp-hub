"""Нагрузочный smoke: 100 одновременных SSE-потоков через мок upstream (R-N4).

Тест помечен маркером ``load`` и в обычном прогоне не выполняется (см. ``pytest_collection_modifyitems``
в ``tests/conftest.py``); запуск — ``pytest -m load``.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

import httpx
import pytest

from tests.conftest import Hub, HubFactory
from tests.support import (
    CATALOG_ENV,
    SSE_MEDIA_TYPE,
    asgi_stream,
    connected_client,
    i3_catalog,
    jsonrpc_body,
    mcp_headers,
    sse_body,
    sse_event,
)

STREAMS = 100


def _sse_response(gate: asyncio.Event) -> httpx.Response:
    event = sse_body(sse_event({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}))

    async def body() -> Any:
        yield event
        await gate.wait()
        yield event

    return httpx.Response(200, headers={"content-type": SSE_MEDIA_TYPE}, content=body())


@pytest.mark.load
@pytest.mark.ac("AC-146")
async def test_hundred_parallel_sse_streams(make_hub: HubFactory) -> None:
    hub: Hub = await make_hub(
        catalog=i3_catalog(),
        env=CATALOG_ENV,
        base_url="https://hub.test",
        max_sse_per_user=STREAMS,
        rate_limit_mcp=STREAMS * 2,
    )
    _conn, tokens = await connected_client(hub)
    headers = mcp_headers(tokens["access_token"], accept=SSE_MEDIA_TYPE)
    gate = asyncio.Event()
    for _ in range(STREAMS):
        hub.upstream.push(_sse_response(gate))

    async with AsyncExitStack() as stack:
        streams = []
        for _ in range(STREAMS):
            stream = await stack.enter_async_context(
                asgi_stream(
                    hub.app,
                    "POST",
                    "/mcp/gitlab",
                    headers=headers,
                    content=jsonrpc_body("tools/list"),
                )
            )
            assert stream.status_code == 200
            streams.append(stream)
        firsts = await asyncio.gather(*(s.next_chunk(timeout=10.0) for s in streams))
        assert all(chunk for chunk in firsts)
        assert await hub.app.state.kv.get("sse:u1") == STREAMS

        gate.set()
        await asyncio.gather(*(s.read_all(timeout=10.0) for s in streams))

    for _ in range(50):
        if await hub.app.state.kv.get("sse:u1") == 0:
            break
        await asyncio.sleep(0.02)
    assert await hub.app.state.kv.get("sse:u1") == 0
    assert hub.upstream.calls == STREAMS
