"""Фильтр инструментов и права подключения (R-P8, R-B7): AC-122..AC-124, AC-150."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.conftest import Hub, HubFactory
from tests.support import (
    CATALOG_ENV,
    catalog_doc,
    connected_client,
    gitlab_facade,
    i3_catalog,
    jira_facade,
    jsonrpc_body,
    mcp_headers,
    native_server,
)

CODE_TOOL_FORBIDDEN = -32001
ALL_TOOLS = ["list_mrs", "create_merge_request", "admin_labels"]


async def _hub(make_hub: HubFactory, *, catalog: dict[str, Any] | None = None, **overrides: Any) -> Hub:
    return await make_hub(
        catalog=catalog if catalog is not None else i3_catalog(),
        env=CATALOG_ENV,
        base_url="https://hub.test",
        **overrides,
    )


def _open_catalog() -> dict[str, Any]:
    """Каталог без ``tool_filter`` и без ``tools`` у групп — фильтрация не применяется."""
    server = gitlab_facade(
        permission_model={
            "kind": "header_groups",
            "header": "Enabled-Groups",
            "always": ["core"],
            "groups": [
                {"id": "code_review", "title": "Code review", "preset": "readonly"},
                {"id": "repo_write", "title": "Запись", "preset": "readwrite"},
            ],
        }
    )
    return catalog_doc([server, jira_facade(), native_server("tag")])


def _tool_names(payload: Any) -> list[str]:
    return [tool["name"] for tool in payload["result"]["tools"]]


def _sse_payload(text: str) -> Any:
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    raise AssertionError(f"в SSE-ответе нет data: {text!r}")


# --- AC-122 ----------------------------------------------------------------


@pytest.mark.ac("AC-122")
async def test_tools_list_hides_unavailable_tools_json(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub, preset="readonly", groups=("code_review",))
    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list"),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert response.status_code == 200, response.text
    assert _tool_names(response.json()) == ["list_mrs"]


@pytest.mark.ac("AC-122")
async def test_tools_list_hides_unavailable_tools_sse(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    hub.upstream.sse_tools_list = True
    _conn, tokens = await connected_client(hub, preset="readonly", groups=("code_review",))
    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list"),
        headers=mcp_headers(tokens["access_token"], accept="text/event-stream"),
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert _tool_names(_sse_payload(response.text)) == ["list_mrs"]


@pytest.mark.ac("AC-122")
async def test_repo_write_group_shows_its_tools(make_hub: HubFactory) -> None:
    """Граница: включённая группа repo_write возвращает create_* (deny admin_* остаётся)."""
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub, preset="readwrite", groups=("repo_write",))
    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list"),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert response.status_code == 200, response.text
    assert _tool_names(response.json()) == ["list_mrs", "create_merge_request"]


@pytest.mark.ac("AC-122")
async def test_catalog_without_filters_shows_all_tools(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, catalog=_open_catalog())
    _conn, tokens = await connected_client(hub, preset="readonly", groups=("code_review",))
    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/list"),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert response.status_code == 200, response.text
    assert _tool_names(response.json()) == ALL_TOOLS


# --- AC-123 ----------------------------------------------------------------


@pytest.mark.ac("AC-123")
async def test_forbidden_tools_call_is_rejected(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub, preset="readonly", groups=("code_review",))
    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/call", {"name": "create_merge_request", "arguments": {}}, request_id=7),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 7
    error = body["error"]
    assert error["code"] == CODE_TOOL_FORBIDDEN
    assert "create_merge_request" in error["message"]
    assert any("Ѐ" <= ch <= "ӿ" for ch in error["message"])
    assert error["data"] == {
        "tool": "create_merge_request",
        "hint_url": "https://hub.test/ui/servers/gitlab",
    }
    assert hub.upstream.calls == 0


@pytest.mark.ac("AC-123")
async def test_allowed_tools_call_reaches_upstream(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub, preset="readonly", groups=("code_review",))
    response = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/call", {"name": "list_mrs", "arguments": {}}, request_id=8),
        headers=mcp_headers(tokens["access_token"]),
    )
    assert response.status_code == 200, response.text
    assert "result" in response.json()
    assert hub.upstream.calls == 1


# --- AC-124 ----------------------------------------------------------------


@pytest.mark.ac("AC-124")
async def test_batch_with_forbidden_call_is_rejected(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub)
    _conn, tokens = await connected_client(hub, preset="readonly", groups=("code_review",))
    batch = json.dumps(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "admin_labels"}},
        ]
    ).encode("utf-8")
    response = await hub.post(
        "/mcp/gitlab", content=batch, headers=mcp_headers(tokens["access_token"])
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    assert body["id"] == 2
    assert body["error"]["code"] == CODE_TOOL_FORBIDDEN
    assert hub.upstream.calls == 0


# --- AC-150 ----------------------------------------------------------------

OVERLAPPING_TOOLS = [
    {"name": "create_issue", "description": "Создать задачу"},
    {"name": "create_merge_request", "description": "Создать MR"},
    {"name": "list_mrs", "description": "Список merge requests"},
]


def _overlapping_masks_catalog() -> dict[str, Any]:
    """Пересекающиеся маски: включаемая группа issues ['create_issue'] и repo_write ['create_*'].

    Маска выключенной группы шире маски включённой, но не совпадает с ней строкой — решение
    должно приниматься по имени инструмента (R-P8 ревизии 2.1).
    """
    server = gitlab_facade(
        permission_model={
            "kind": "header_groups",
            "header": "Enabled-Groups",
            "always": [],
            "groups": [
                {
                    "id": "issues",
                    "title": "Задачи",
                    "preset": "readonly",
                    "tools": ["create_issue"],
                },
                {
                    "id": "repo_write",
                    "title": "Запись в репозиторий",
                    "preset": "readonly",
                    "tools": ["create_*"],
                },
            ],
            "tool_filter": {"allow": ["*"]},
        }
    )
    return catalog_doc([server, jira_facade(), native_server("tag")])


@pytest.mark.ac("AC-150")
async def test_overlapping_group_masks_resolved_by_tool_name(make_hub: HubFactory) -> None:
    hub = await _hub(make_hub, catalog=_overlapping_masks_catalog())
    hub.upstream.tools = [dict(tool) for tool in OVERLAPPING_TOOLS]
    _conn, tokens = await connected_client(hub, preset="readonly", groups=("issues",))
    headers = mcp_headers(tokens["access_token"])

    listed = await hub.post("/mcp/gitlab", content=jsonrpc_body("tools/list"), headers=headers)
    assert listed.status_code == 200, listed.text
    assert _tool_names(listed.json()) == ["create_issue", "list_mrs"]

    calls_before = hub.upstream.calls
    granted = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body("tools/call", {"name": "create_issue", "arguments": {}}, request_id=2),
        headers=headers,
    )
    assert granted.status_code == 200, granted.text
    body = granted.json()
    assert "error" not in body
    assert body["result"]["content"][0]["text"] == "вызван create_issue"
    assert hub.upstream.calls == calls_before + 1

    calls_before = hub.upstream.calls
    refused = await hub.post(
        "/mcp/gitlab",
        content=jsonrpc_body(
            "tools/call", {"name": "create_merge_request", "arguments": {}}, request_id=3
        ),
        headers=headers,
    )
    assert refused.status_code == 200, refused.text
    error = refused.json()["error"]
    assert error["code"] == CODE_TOOL_FORBIDDEN
    assert error["data"] == {
        "tool": "create_merge_request",
        "hint_url": "https://hub.test/ui/servers/gitlab",
    }
    assert hub.upstream.calls == calls_before
