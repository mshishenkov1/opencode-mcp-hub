"""MCP-proxy (R-P1..R-P11): AC-114..AC-121, AC-125..AC-130, AC-151, AC-152."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from typing import Any

import httpx
import pytest

from hub.crypto import jwt_decode, jwt_encode
from hub.proxy import SESSION_PREFIX
from tests.conftest import Hub, HubFactory
from tests.support import (
    CATALOG_ENV,
    INITIALIZE_PARAMS,
    SSE_MEDIA_TYPE,
    UPSTREAM_ACCESS,
    asgi_stream,
    connected_client,
    execute,
    i3_catalog,
    jsonrpc_body,
    mcp_headers,
    refresh_grant,
    sse_body,
    sse_event,
    web_login,
)

CODE_SESSION = -32000
CODE_CONNECTION = -32002
CODE_RATE_LIMIT = -32003
CODE_UPSTREAM = -32004

TWO_TOOLS = [{"name": "list_mrs"}, {"name": "list_issues"}]
CALL_BODY = jsonrpc_body("tools/call", {"name": "list_mrs"})


async def _hub(make_hub: HubFactory, **overrides: Any) -> Hub:
    return await make_hub(
        catalog=i3_catalog(), env=CATALOG_ENV, base_url="https://hub.test", **overrides
    )


async def _initialize(hub: Hub, token: str) -> str:
    """Выполнить initialize и вернуть клиентский ``Mcp-Session-Id``."""
    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("initialize", INITIALIZE_PARAMS),
        headers=mcp_headers(token),
    )
    assert response.status_code == 200, response.text
    session_id = response.headers["Mcp-Session-Id"]
    assert session_id
    return session_id


def _sse_response(events: list[bytes], gate: asyncio.Event | None = None, **headers: str) -> httpx.Response:
    async def body() -> Any:
        yield events[0]
        if gate is not None:
            await gate.wait()
        for chunk in events[1:]:
            yield chunk

    return httpx.Response(
        200, headers={"content-type": SSE_MEDIA_TYPE, **headers}, content=body()
    )


# --- AC-114 ----------------------------------------------------------------


@pytest.mark.ac("AC-114")
async def test_initialize_and_tools_list_are_proxied(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    hub.upstream.tools = list(TWO_TOOLS)
    _conn, tokens = await connected_client(hub)
    token = tokens["access_token"]

    init_body = jsonrpc_body("initialize", INITIALIZE_PARAMS)
    init = await hub.post("/mcp/gitlab", content=init_body, headers=mcp_headers(token))
    assert init.status_code == 200, init.text
    assert init.headers["content-type"].startswith("application/json")
    assert init.json()["result"]["serverInfo"]["name"] == "mock-upstream"
    session_id = init.headers["Mcp-Session-Id"]

    list_body = jsonrpc_body("tools/list", request_id=2)
    listed = await hub.post(
        "/mcp/gitlab", content=list_body, headers=mcp_headers(token, session_id=session_id)
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["result"]["tools"] == TWO_TOOLS

    assert hub.upstream.calls == 2
    first, second = hub.upstream.requests
    assert first.method == second.method == "POST"
    assert first.url == second.url == "https://mcp-gitlab.internal.test/mcp"
    assert json.loads(first.content) == json.loads(init_body)
    assert json.loads(second.content) == json.loads(list_body)


# --- AC-115 ----------------------------------------------------------------


@pytest.mark.ac("AC-115")
async def test_upstream_headers_are_rewritten(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub, groups=("devops", "code_review"))
    headers = {
        "Authorization": f"Bearer {tokens['access_token']}",
        "Cookie": "a=b",
        "X-Forwarded-For": "1.2.3.4",
        "Enabled-Groups": "admin",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
        "Content-Type": "application/json",
    }
    response = await hub.post("/mcp/gitlab", content=CALL_BODY, headers=headers)
    assert response.status_code == 200, response.text

    sent = hub.upstream.last()
    assert sent.header("authorization") == f"Bearer {UPSTREAM_ACCESS}"
    assert sent.header("x-static") == "st-1"
    assert sent.header("enabled-groups") == "core,code_review,devops"
    assert sent.header("accept") == "application/json, text/event-stream"
    assert sent.header("mcp-protocol-version") == "2025-06-18"
    assert sent.header("cookie") is None
    assert sent.header("x-forwarded-for") is None
    assert tokens["access_token"] not in str(sent.headers)


@pytest.mark.ac("AC-115")
async def test_client_authorization_is_dropped_when_catalog_sets_other_header(
    make_hub: HubFactory,
) -> None:
    """Токен Hub не уходит на upstream, даже если каталог не задаёт свой ``Authorization``.

    У 'jira' (как у jira/confluence боевого каталога) креды идут в собственном заголовке
    ``X-Atlassian-Jira-Personal-Token``, поэтому подставленное значение не «затирает» возможный
    проброс клиентского ``Authorization`` — только удаление заголовка проксёй (R-P2) не даёт
    access-токену Hub уйти на чужой upstream.
    """
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub, alias="jira", groups=("issues",))
    token = tokens["access_token"]
    response = await hub.post(
        "/mcp/jira",
        content=CALL_BODY,
        headers={
            "Authorization": f"Bearer {token}",
            "Cookie": "a=b",
            "X-Forwarded-For": "1.2.3.4",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200, response.text

    assert hub.net is not None
    sent = hub.net.upstreams["jira"].last()
    assert sent.header("x-atlassian-jira-personal-token") == UPSTREAM_ACCESS
    assert sent.header("x-atlassian-jira-url") == "https://jira.test"
    assert sent.header("accept") == "application/json, text/event-stream"
    assert sent.header("mcp-protocol-version") == "2025-06-18"
    assert sent.header("authorization") is None
    assert sent.header("cookie") is None
    assert sent.header("x-forwarded-for") is None
    # Токен Hub не пришёл ни под каким именем заголовка.
    leaked = [name for name, value in sent.headers.items() if token in value]
    assert leaked == [], f"токен Hub ушёл на upstream в заголовках {leaked}"


# --- AC-116 ----------------------------------------------------------------


@pytest.mark.ac("AC-116")
async def test_sse_response_is_streamed(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, upstream_timeout=1)
    _conn, tokens = await connected_client(hub)
    gate = asyncio.Event()
    events = [
        sse_body(sse_event({"jsonrpc": "2.0", "id": 1, "result": {"step": 1}})),
        sse_body(sse_event({"jsonrpc": "2.0", "id": 1, "result": {"step": 2}})),
        sse_body(sse_event({"jsonrpc": "2.0", "id": 1, "result": {"step": 3}})),
    ]
    hub.upstream.push(_sse_response(events, gate, **{"mcp-session-id": "up-1"}))

    headers = mcp_headers(tokens["access_token"], accept=SSE_MEDIA_TYPE)
    async with asgi_stream(
        hub.app, "POST", "/mcp/gitlab", headers=headers, content=jsonrpc_body("tools/list")
    ) as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith(SSE_MEDIA_TYPE)
        assert stream.headers.get("mcp-session-id") not in (None, "up-1")
        first = await stream.next_chunk()
        assert first is not None
        assert b'"step": 1' in first or b'"step":1' in first
        assert not gate.is_set()
        gate.set()
        rest = await stream.read_all()
    assert b"step" in rest
    assert stream.chunks[0] == first
    assert len(stream.chunks) >= 2


@pytest.mark.ac("AC-116")
async def test_upstream_timeout_returns_502(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, upstream_timeout=1)
    _conn, tokens = await connected_client(hub)
    hub.upstream.push(httpx.ConnectTimeout("upstream молчит"))
    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list", request_id=4),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert response.status_code == 502, response.text
    error = response.json()["error"]
    assert error["code"] == CODE_UPSTREAM
    assert error["data"]["hint_url"] == "https://hub.test/ui/servers/gitlab"


# --- AC-117 ----------------------------------------------------------------


@pytest.mark.ac("AC-117")
async def test_client_sees_hub_session_id(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub)
    token = tokens["access_token"]
    session_id = await _initialize(hub, token)
    assert session_id != "up-1"

    listed = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list", request_id=2),
        headers=mcp_headers(token, session_id=session_id),
    )
    assert listed.status_code == 200, listed.text
    assert hub.upstream.last().header("mcp-session-id") == "up-1"

    unknown = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list", request_id=3),
        headers=mcp_headers(token, session_id="unknown-1"),
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == CODE_SESSION


@pytest.mark.ac("AC-117")
async def test_session_of_other_user_is_not_found(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub, user_id="u1")
    _other, other_tokens = await connected_client(hub, user_id="u2")
    session_id = await _initialize(hub, tokens["access_token"])

    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list", request_id=9),
        headers=mcp_headers(other_tokens["access_token"], session_id=session_id),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == CODE_SESSION


# --- AC-118 ----------------------------------------------------------------


@pytest.mark.ac("AC-118")
async def test_idle_upstream_session_is_recreated(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, upstream_idle_ttl=600)
    _conn, tokens = await connected_client(hub)
    token = tokens["access_token"]
    session_id = await _initialize(hub, token)

    hub.clock.advance(601)
    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list", request_id=2),
        headers=mcp_headers(token, session_id=session_id),
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["tools"] is not None
    assert response.headers["Mcp-Session-Id"] == session_id

    methods = [
        (r.method, (r.json_body or {}).get("method"), r.header("mcp-session-id"))
        for r in hub.upstream.requests[1:]
    ]
    assert ("DELETE", None, "up-1") in methods
    reinit = next(r for r in hub.upstream.requests[1:] if (r.json_body or {}).get("method") == "initialize")
    params = reinit.json_body["params"]
    assert params["clientInfo"] == {"name": "opencode", "version": "1.17.9"}
    assert params["protocolVersion"] == "2025-06-18"
    assert ("POST", "notifications/initialized", "up-2") in methods
    assert ("POST", "tools/list", "up-2") in methods


# --- AC-119 ----------------------------------------------------------------


@pytest.mark.ac("AC-119")
async def test_upstream_404_triggers_single_recreation(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub)
    token = tokens["access_token"]
    session_id = await _initialize(hub, token)
    # upstream «забыл» сессию up-1 — на следующий запрос ответит 404.
    hub.upstream.sessions.discard("up-1")

    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list", request_id=2),
        headers=mcp_headers(token, session_id=session_id),
    )
    assert response.status_code == 200, response.text
    assert "result" in response.json()
    initializes = [
        r for r in hub.upstream.requests if (r.json_body or {}).get("method") == "initialize"
    ]
    assert len(initializes) == 2


@pytest.mark.ac("AC-119")
async def test_repeated_404_after_recreation_gives_upstream_error(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub)
    token = tokens["access_token"]
    session_id = await _initialize(hub, token)

    not_found = httpx.Response(404, json={"error": "session_not_found"})
    hub.upstream.push(not_found)  # tools/list с up-1
    hub.upstream.push(httpx.Response(204))  # DELETE старой сессии
    hub.upstream.push(
        httpx.Response(200, json={"jsonrpc": "2.0", "id": "x", "result": {}},
                       headers={"mcp-session-id": "up-2"})
    )
    hub.upstream.push(httpx.Response(202))  # notifications/initialized
    hub.upstream.push(not_found)  # повторный tools/list

    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list", request_id=2),
        headers=mcp_headers(token, session_id=session_id),
    )
    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == CODE_UPSTREAM
    assert hub.upstream.calls == 6  # initialize + 5 запросов сценария


# --- AC-120 ----------------------------------------------------------------


@pytest.mark.ac("AC-120")
async def test_delete_closes_session(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub)
    token = tokens["access_token"]
    session_id = await _initialize(hub, token)

    deleted = await hub.client.delete(
        "/mcp/gitlab", headers=mcp_headers(token, session_id=session_id)
    )
    assert deleted.status_code in (200, 204), deleted.text
    last = hub.upstream.last()
    assert last.method == "DELETE"
    assert last.header("mcp-session-id") == "up-1"
    assert await hub.app.state.kv.get(SESSION_PREFIX + session_id) is None

    after = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list", request_id=3),
        headers=mcp_headers(token, session_id=session_id),
    )
    assert after.status_code == 404
    assert after.json()["error"]["code"] == CODE_SESSION


# --- AC-121 ----------------------------------------------------------------


@pytest.mark.ac("AC-121")
async def test_tools_cache_ttl_permissions_and_reload(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, tools_cache_ttl=300, admin_token="adm")
    csrf = await web_login(hub)
    _conn, tokens = await connected_client(hub)
    headers = mcp_headers(tokens["access_token"])

    first = await hub.post("/mcp/gitlab", content=jsonrpc_body("tools/list"), headers=headers)
    assert first.status_code == 200
    assert hub.upstream.calls == 1

    second = await hub.post(
        "/mcp/gitlab", content=jsonrpc_body("tools/list", request_id=2), headers=headers
    )
    assert second.status_code == 200
    assert second.json()["id"] == 2
    assert hub.upstream.calls == 1

    hub.clock.advance(301)
    third = await hub.post(
        "/mcp/gitlab", content=jsonrpc_body("tools/list", request_id=3), headers=headers
    )
    assert third.status_code == 200
    assert hub.upstream.calls == 2

    changed = await hub.client.put(
        "/api/me/connections/gitlab/permissions",
        json={"preset": "readonly", "groups": ["code_review", "devops"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert changed.status_code == 200, changed.text
    fourth = await hub.post(
        "/mcp/gitlab", content=jsonrpc_body("tools/list", request_id=4), headers=headers
    )
    assert fourth.status_code == 200
    assert hub.upstream.calls == 3

    reload = await hub.post("/admin/catalog/reload", headers={"X-Admin-Token": "adm"})
    assert reload.status_code == 200, reload.text
    fifth = await hub.post(
        "/mcp/gitlab", content=jsonrpc_body("tools/list", request_id=5), headers=headers
    )
    assert fifth.status_code == 200
    assert hub.upstream.calls == 4


@pytest.mark.ac("AC-121")
async def test_tools_list_with_cursor_is_not_cached(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, tools_cache_ttl=300)
    _conn, tokens = await connected_client(hub)
    headers = mcp_headers(tokens["access_token"])
    for request_id in (1, 2):
        response = await hub.post(
            "/mcp/gitlab",
            content=jsonrpc_body("tools/list", {"cursor": "c1"}, request_id=request_id),
            headers=headers,
        )
        assert response.status_code == 200, response.text
    assert hub.upstream.calls == 2


# --- AC-125 ----------------------------------------------------------------


@pytest.mark.ac("AC-125")
async def test_rate_limit_per_user_and_alias(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, rate_limit_mcp=2)
    _conn, tokens = await connected_client(hub)
    _jira, jira_tokens = await connected_client(hub, alias="jira", groups=("issues",))
    headers = mcp_headers(tokens["access_token"])

    for request_id in (1, 2):
        assert (
            await hub.post(
                "/mcp/gitlab", content=jsonrpc_body("tools/call", {"name": "list_mrs"}, request_id=request_id), headers=headers
            )
        ).status_code == 200
    before = hub.upstream.calls

    limited = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/call", {"name": "list_mrs"}, request_id=3),
        headers=headers,
    )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == CODE_RATE_LIMIT
    assert int(limited.headers["Retry-After"]) >= 1
    assert hub.upstream.calls == before

    other = await hub.post(
        "/mcp/jira",
        content=jsonrpc_body("tools/call", {"name": "list_issues"}),
        headers=mcp_headers(jira_tokens["access_token"]),
    )
    assert other.status_code == 200, other.text

    hub.clock.advance(61)
    again = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/call", {"name": "list_mrs"}, request_id=4),
        headers=headers,
    )
    assert again.status_code == 200, again.text


# --- AC-126 ----------------------------------------------------------------


@pytest.mark.ac("AC-126")
async def test_concurrent_sse_streams_are_limited(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, max_sse_per_user=2)
    _conn, tokens = await connected_client(hub)
    headers = mcp_headers(tokens["access_token"], accept=SSE_MEDIA_TYPE)
    gate = asyncio.Event()
    event = sse_body(sse_event({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}))
    for _ in range(4):
        hub.upstream.push(_sse_response([event, event], gate))

    async with AsyncExitStack() as stack:
        streams = []
        for _ in range(2):
            stream = await stack.enter_async_context(
                asgi_stream(
                    hub.app, "POST", "/mcp/gitlab", headers=headers,
                    content=jsonrpc_body("tools/list"),
                )
            )
            assert stream.status_code == 200
            assert await stream.next_chunk() is not None
            streams.append(stream)

        async with asgi_stream(
            hub.app, "POST", "/mcp/gitlab", headers=headers, content=jsonrpc_body("tools/list")
        ) as third:
            assert third.status_code == 429
            body = json.loads(await third.read_all())
            assert body["error"]["code"] == CODE_RATE_LIMIT
            assert body["error"]["data"]["reason"] == "too_many_streams"

        gate.set()
        await streams[0].read_all()

    async with asgi_stream(
        hub.app, "POST", "/mcp/gitlab", headers=headers, content=jsonrpc_body("tools/list")
    ) as fourth:
        assert fourth.status_code == 200
        assert await fourth.next_chunk() is not None


# --- AC-127 ----------------------------------------------------------------


@pytest.mark.ac("AC-127")
async def test_large_body_with_content_length_is_rejected(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, max_body_bytes=1024)
    _conn, tokens = await connected_client(hub)
    response = await hub.post(
        "/mcp/gitlab", content=b"x" * 2048, headers=mcp_headers(tokens["access_token"])
    )
    assert response.status_code == 413, response.text
    assert response.json()["error"] == "payload_too_large"
    assert response.json()["message"]
    assert hub.upstream.calls == 0


@pytest.mark.ac("AC-127")
async def test_large_chunked_body_is_rejected(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, max_body_bytes=1024)
    _conn, tokens = await connected_client(hub)

    async def chunks() -> Any:
        for _ in range(4):
            yield b"x" * 512

    response = await hub.post(
        "/mcp/gitlab", content=chunks(), headers=mcp_headers(tokens["access_token"])
    )
    assert response.status_code == 413, response.text
    assert response.json()["error"] == "payload_too_large"
    assert hub.upstream.calls == 0


# --- AC-128 ----------------------------------------------------------------


@pytest.mark.ac("AC-128")
async def test_circuit_breaker_opens_and_recovers(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, cb_failures=3, cb_reset=30)
    _conn, tokens = await connected_client(hub)
    headers = mcp_headers(tokens["access_token"])
    hub.upstream.push_many(httpx.Response(500, json={"error": "boom"}), 3)

    for request_id in (1, 2, 3):
        response = await hub.post(
            "/mcp/gitlab",
            content=jsonrpc_body("tools/call", {"name": "list_mrs"}, request_id=request_id),
            headers=headers,
        )
        assert response.status_code == 502, response.text
        assert response.json()["error"]["code"] == CODE_UPSTREAM
    assert hub.upstream.calls == 3

    opened = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/call", {"name": "list_mrs"}, request_id=4),
        headers=headers,
    )
    assert opened.status_code == 503, opened.text
    error = opened.json()["error"]
    assert error["code"] == CODE_UPSTREAM
    assert error["data"]["reason"] == "upstream_unavailable"
    assert int(opened.headers["Retry-After"]) >= 1
    assert hub.upstream.calls == 3

    hub.clock.advance(31)
    probe = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/call", {"name": "list_mrs"}, request_id=5),
        headers=headers,
    )
    assert probe.status_code == 200, probe.text
    assert hub.upstream.calls == 4

    again = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/call", {"name": "list_mrs"}, request_id=6),
        headers=headers,
    )
    assert again.status_code == 200, again.text
    assert hub.upstream.calls == 5


# --- AC-151 ----------------------------------------------------------------


def _call(request_id: int) -> bytes:
    return jsonrpc_body("tools/call", {"name": "list_mrs"}, request_id=request_id)


async def _open_breaker(hub: Hub, headers: dict[str, str]) -> None:
    """Три неуспешных вызова подряд открывают окно (HUB_CB_FAILURES=3)."""
    hub.upstream.push_many(httpx.Response(500, json={"error": "boom"}), 3)
    for request_id in (1, 2, 3):
        response = await hub.post("/mcp/gitlab", content=_call(request_id), headers=headers)
        assert response.status_code == 502, response.text
    assert hub.upstream.calls == 3


@pytest.mark.ac("AC-151")
async def test_failed_half_open_probe_reopens_window(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, cb_failures=3, cb_reset=30)
    _conn, tokens = await connected_client(hub)
    headers = mcp_headers(tokens["access_token"])
    await _open_breaker(hub, headers)

    # Пробный запрос уходит на upstream и снова получает 500.
    hub.clock.advance(31)
    hub.upstream.push(httpx.Response(500, json={"error": "boom"}))
    probe = await hub.post("/mcp/gitlab", content=_call(4), headers=headers)
    assert probe.status_code == 502, probe.text
    assert probe.json()["error"]["code"] == CODE_UPSTREAM
    assert hub.upstream.calls == 4

    # Окно открылось снова сразу: повторно накапливать HUB_CB_FAILURES ошибок не требуется.
    for request_id in (5, 6):
        blocked = await hub.post("/mcp/gitlab", content=_call(request_id), headers=headers)
        assert blocked.status_code == 503, blocked.text
        error = blocked.json()["error"]
        assert error["code"] == CODE_UPSTREAM
        assert error["data"]["reason"] == "upstream_unavailable"
        assert int(blocked.headers["Retry-After"]) >= 1
    assert hub.upstream.calls == 4

    # Новое истечение окна: успешная проба закрывает выключатель.
    hub.clock.advance(31)
    recovered = await hub.post("/mcp/gitlab", content=_call(7), headers=headers)
    assert recovered.status_code == 200, recovered.text
    assert hub.upstream.calls == 5

    following = await hub.post("/mcp/gitlab", content=_call(8), headers=headers)
    assert following.status_code == 200, following.text
    assert hub.upstream.calls == 6


# --- AC-152 ----------------------------------------------------------------


@pytest.mark.ac("AC-152")
async def test_only_one_request_probes_upstream_in_half_open(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, cb_failures=3, cb_reset=30)
    _conn, tokens = await connected_client(hub)
    headers = mcp_headers(tokens["access_token"])
    await _open_breaker(hub, headers)

    gate = asyncio.Event()

    async def hold(recorded: Any) -> httpx.Response:
        await gate.wait()
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": recorded.json_body["id"], "result": {"ok": True}},
        )

    hub.upstream.push(hold)
    hub.clock.advance(31)

    tasks = [
        asyncio.create_task(hub.post("/mcp/gitlab", content=_call(request_id), headers=headers))
        for request_id in (4, 5, 6)
    ]
    try:
        async with asyncio.timeout(5):
            while sum(task.done() for task in tasks) < 2:
                await asyncio.sleep(0)
        # Пока проба удерживается upstream'ом, остальные запросы уже отказаны.
        assert hub.upstream.calls == 4, "во время пробы на upstream ушёл не один запрос"
        refused = [task.result() for task in tasks if task.done()]
        for response in refused:
            assert response.status_code == 503, response.text
            error = response.json()["error"]
            assert error["code"] == CODE_UPSTREAM
            assert error["data"]["reason"] == "upstream_unavailable"
            assert int(response.headers["Retry-After"]) >= 1
    finally:
        gate.set()
        responses = await asyncio.gather(*tasks)

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 503, 503]
    assert hub.upstream.calls == 4

    # Успешная проба закрыла выключатель и обнулила счётчик ошибок.
    following = await hub.post("/mcp/gitlab", content=_call(7), headers=headers)
    assert following.status_code == 200, following.text
    assert hub.upstream.calls == 5
    state = await hub.app.state.kv.get("cb:gitlab")
    assert state == {"failures": 0, "open_until": 0.0}


# --- AC-129 ----------------------------------------------------------------


@pytest.mark.ac("AC-129")
async def test_missing_connection_returns_jsonrpc_error(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    conn, tokens = await connected_client(hub)
    await execute(hub.app, "DELETE FROM connections WHERE id = :cid", cid=conn.id)
    await hub.app.state.broker.invalidate_cache("u1", "gitlab")
    headers = mcp_headers(tokens["access_token"])

    post = await hub.post(
        "/mcp/gitlab", content=jsonrpc_body("tools/list", request_id=3), headers=headers
    )
    assert post.status_code == 200, post.text
    body = post.json()
    assert body["id"] == 3
    assert body["error"]["code"] == CODE_CONNECTION
    assert body["error"]["message"]
    assert body["error"]["data"] == {
        "reason": "not_connected",
        "hint_url": "https://hub.test/ui/servers/gitlab",
    }

    get = await hub.get("/mcp/gitlab", headers=headers)
    assert get.status_code == 200, get.text
    plain = get.json()
    assert plain["error"] == "not_connected"
    assert plain["message"]
    assert plain["hint_url"] == "https://hub.test/ui/servers/gitlab"
    assert hub.upstream.calls == 0


# --- AC-130 ----------------------------------------------------------------


@pytest.mark.ac("AC-130")
async def test_revoked_and_expired_tokens_are_rejected(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub)
    token = tokens["access_token"]
    secret = hub.settings.secret_key.get_secret_value()
    claims = jwt_decode(token, secret)
    expired = jwt_encode({**claims, "exp": int(hub.clock.time()) - 1}, secret)

    assert (await hub.post("/oauth/revoke", data={"token": token})).status_code == 200

    for candidate in (token, expired):
        response = await hub.post(
            "/mcp/gitlab",
            content=jsonrpc_body("tools/list"),
            headers=mcp_headers(candidate),
        )
        assert response.status_code == 401, response.text
        assert response.json()["error"] == "unauthorized"
        header = response.headers["WWW-Authenticate"]
        assert "Bearer" in header
        assert ".well-known/oauth-protected-resource/mcp/gitlab" in header
        assert 'error="invalid_token"' in header
    assert hub.upstream.calls == 0

    refreshed = await refresh_grant(
        hub, refresh_token=tokens["refresh_token"], client_id=tokens["client_id"]
    )
    assert refreshed.status_code == 400


@pytest.mark.ac("AC-126")
async def test_sse_counter_released_on_client_disconnect(make_hub: HubFactory) -> None:
    """Разрыв соединения клиентом освобождает слот SSE-потока (R-P9)."""
    hub = await _hub(make_hub, max_sse_per_user=1)
    _conn, tokens = await connected_client(hub)
    headers = mcp_headers(tokens["access_token"], accept=SSE_MEDIA_TYPE)
    gate = asyncio.Event()
    event = sse_body(sse_event({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}))
    for _ in range(3):
        hub.upstream.push(_sse_response([event, event], gate))

    async with asgi_stream(
        hub.app, "POST", "/mcp/gitlab", headers=headers, content=jsonrpc_body("tools/list")
    ) as first:
        assert first.status_code == 200
        assert await first.next_chunk() is not None
        async with asgi_stream(
            hub.app, "POST", "/mcp/gitlab", headers=headers, content=jsonrpc_body("tools/list")
        ) as blocked:
            assert blocked.status_code == 429
        first.disconnect()
        await asyncio.sleep(0.05)

    for _ in range(20):
        if await hub.app.state.kv.get(f"sse:{'u1'}") == 0:
            break
        await asyncio.sleep(0.02)
    assert await hub.app.state.kv.get("sse:u1") == 0

    async with asgi_stream(
        hub.app, "POST", "/mcp/gitlab", headers=headers, content=jsonrpc_body("tools/list")
    ) as again:
        assert again.status_code == 200
        assert await again.next_chunk() is not None


# --- дополнительные ветки proxy -------------------------------------------


@pytest.mark.ac("AC-116")
async def test_get_returns_sse_stream(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub)
    token = tokens["access_token"]
    session_id = await _initialize(hub, token)
    event = sse_body(sse_event({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}))
    hub.upstream.push(_sse_response([event]))

    response = await hub.get(
        "/mcp/gitlab", headers=mcp_headers(token, session_id=session_id, accept=SSE_MEDIA_TYPE)
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(SSE_MEDIA_TYPE)
    assert response.headers["Mcp-Session-Id"] == session_id
    assert b'"ok"' in response.content


@pytest.mark.ac("AC-116")
async def test_get_passes_through_non_sse_response(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub)
    token = tokens["access_token"]
    session_id = await _initialize(hub, token)
    response = await hub.get("/mcp/gitlab", headers=mcp_headers(token, session_id=session_id))
    assert response.status_code == 405
    assert response.json()["error"] == "method_not_allowed"


@pytest.mark.ac("AC-116")
async def test_sse_idle_timeout_closes_stream(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, upstream_sse_idle_timeout=0.05)
    _conn, tokens = await connected_client(hub)
    gate = asyncio.Event()
    event = sse_body(sse_event({"jsonrpc": "2.0", "id": 1, "result": {"step": 1}}))
    hub.upstream.push(_sse_response([event, event], gate))

    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list"),
        headers=mcp_headers(tokens["access_token"], accept=SSE_MEDIA_TYPE),
    )
    assert response.status_code == 200
    assert response.content.count(b"data:") == 1
    gate.set()


@pytest.mark.ac("AC-116")
async def test_network_error_returns_502(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub)
    hub.upstream.push(httpx.ConnectError("сеть недоступна"))
    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/call", {"name": "list_mrs"}),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == CODE_UPSTREAM


@pytest.mark.ac("AC-114")
async def test_non_json_upstream_body_is_passed_through(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub)
    hub.upstream.push(
        httpx.Response(200, headers={"content-type": "text/plain"}, content=b"not json")
    )
    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/call", {"name": "list_mrs"}),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert response.status_code == 200
    assert response.content == b"not json"


@pytest.mark.ac("AC-120")
async def test_delete_without_session_header_is_404(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub)
    response = await hub.client.delete(
        "/mcp/gitlab", headers=mcp_headers(tokens["access_token"])
    )
    assert response.status_code == 404
    assert response.json()["error"] == "session_not_found"
    assert hub.upstream.calls == 0


@pytest.mark.ac("AC-120")
async def test_delete_survives_upstream_error(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub)
    token = tokens["access_token"]
    session_id = await _initialize(hub, token)
    hub.upstream.push(httpx.ConnectError("сеть недоступна"))
    response = await hub.client.delete(
        "/mcp/gitlab", headers=mcp_headers(token, session_id=session_id)
    )
    assert response.status_code in (200, 204)
    assert await hub.app.state.kv.get(SESSION_PREFIX + session_id) is None


@pytest.mark.ac("AC-118")
async def test_recreation_failure_returns_upstream_error(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, upstream_idle_ttl=600)
    _conn, tokens = await connected_client(hub)
    token = tokens["access_token"]
    session_id = await _initialize(hub, token)
    hub.clock.advance(601)
    hub.upstream.push(httpx.Response(204))  # DELETE старой сессии
    hub.upstream.push(httpx.Response(500, json={"error": "boom"}))  # повторный initialize
    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list", request_id=2),
        headers=mcp_headers(token, session_id=session_id),
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == CODE_UPSTREAM
