"""``scope`` как множество значений через пробел (R-O5.1, R-O5.2, R-O6.2, R-O6.3, R-O10, R-W3):
AC-203..AC-213.

Класс дефекта BUG-I3-002: Hub объявлял в ``scopes_supported`` обе области коннектора, а сравнивал
``scope`` со строкой целиком, поэтому штатный запрос MCP-клиента ``<alias>:readonly
<alias>:readwrite`` (RFC 6749 §3.3) не проходил. Прежние проверки этого не ловили по двум причинам:
запрашивалась ровно одна область и токен Hub выпускался напрямую, минуя ``/oauth/authorize``.
Поэтому здесь всё идёт штатным путём — ``GET /oauth/authorize`` → экран прав → обмен кода на
``/oauth/token`` — а инвариант R-O5.1 проверяется перебором подмножеств объявленного набора.

Все проверки идут против локальных моков (``MockNetwork``, SQLite ``:memory:``, in-memory KV).
"""

from __future__ import annotations

import copy
import re
from itertools import combinations
from typing import Any

import pytest

from tests.conftest import Hub, HubFactory
from tests.support import (
    CATALOG_ENV,
    JIRA_AS,
    JIRA_UPSTREAM,
    LOOPBACK_REDIRECT,
    PUBLIC_URL,
    TAG_UPSTREAM,
    MockNetwork,
    MockProviderAS,
    MockUpstream,
    add_key,
    authorize_params,
    bearer,
    catalog_doc,
    connected_client,
    dump_kv,
    exchange_code,
    facade_server,
    fetch_rows,
    jsonrpc_body,
    mcp_headers,
    pkce_pair,
    provider_callback,
    query_of,
    refresh_grant,
    register_client,
    seed_connection,
    submit_consent,
    web_login,
    web_logout,
)

TAG_AS = "https://tag-as.test"
CONFLUENCE_AS = "https://confluence-as.test"
CONFLUENCE_UPSTREAM = "https://mcp-confluence.internal.test/mcp"

CODE_TOOL_FORBIDDEN = -32001

# Значения scopes каталога по пресетам не пересекаются: по запросу, ушедшему в целевую систему,
# видно, права какого пресета Hub запросил (AC-203, AC-208, AC-210).
PROVIDER_SCOPES: dict[str, dict[str, list[str]]] = {
    "tag": {
        "readonly": ["tag_read_messages", "tag_read_channels"],
        "readwrite": ["tag_post_messages", "tag_manage_channels"],
    },
    "jira": {"readonly": ["read:jira"], "readwrite": ["write:jira"]},
    "confluence": {"readonly": ["read:confluence"], "readwrite": ["write:confluence"]},
}

ALIASES = ("tag", "jira", "confluence")

TAG_UPSTREAM_TOOLS = [
    {"name": "search_posts", "description": "Поиск сообщений"},
    {"name": "get_thread", "description": "Тред"},
    {"name": "create_post", "description": "Написать сообщение"},
]


# --- каталог и моки --------------------------------------------------------


def _facade(alias: str, *, as_base: str, upstream: str, title: str) -> dict[str, Any]:
    """facade-коннектор со способом oauth2 и непересекающимися scopes пресетов."""
    return facade_server(
        alias,
        title=title,
        description=f"Тестовый коннектор {title}.",
        upstream_url=upstream,
        auth={
            "type": "oauth2",
            "authorize_url": f"{as_base}/oauth/authorize",
            "token_url": f"{as_base}/oauth/token",
            "revoke_url": f"{as_base}/oauth/revoke",
            "client_id": f"{alias}-client-id",
            "client_secret": "env:GL_SECRET",
            "pkce": True,
            "scopes": copy.deepcopy(PROVIDER_SCOPES[alias]),
        },
        credential_headers={"Authorization": "Bearer {{access_token}}"},
        static_headers={},
        permission_model={
            "kind": "header_groups",
            "header": "Enabled-Groups",
            "always": ["core"],
            "groups": [
                {"id": "read_messages", "title": "Чтение", "preset": "readonly"},
                {
                    "id": "post_messages",
                    "title": "Запись",
                    "preset": "readwrite",
                    "tools": ["create_*"],
                },
                {"id": "admin", "title": "Администрирование", "preset": "none"},
            ],
            "tool_filter": {"allow": ["*"], "deny": ["admin_*"]},
        },
    )


def _catalog() -> dict[str, Any]:
    """Каталог трёх facade-коннекторов: 'tag', 'jira', 'confluence'."""
    return catalog_doc(
        [
            _facade("tag", as_base=TAG_AS, upstream=TAG_UPSTREAM, title="ТЭГ"),
            _facade("jira", as_base=JIRA_AS, upstream=JIRA_UPSTREAM, title="Jira"),
            _facade(
                "confluence",
                as_base=CONFLUENCE_AS,
                upstream=CONFLUENCE_UPSTREAM,
                title="Confluence",
            ),
        ]
    )


async def _hub(make_hub: HubFactory, net: MockNetwork, **overrides: Any) -> Hub:
    net.providers["tag"] = MockProviderAS(
        TAG_AS, access_token="tag-access-1", refresh_token="tag-refresh-1", scope=""
    )
    net.providers["confluence"] = MockProviderAS(
        CONFLUENCE_AS, access_token="conf-access-1", refresh_token="conf-refresh-1", scope=""
    )
    net.upstreams["confluence"] = MockUpstream(CONFLUENCE_UPSTREAM, prefix="conf")
    net.upstreams["tag"].tools = copy.deepcopy(TAG_UPSTREAM_TOOLS)
    return await make_hub(
        catalog=_catalog(), env=CATALOG_ENV, base_url="https://hub.test", **overrides
    )


async def _ready_client(hub: Hub, user_id: str = "u1") -> str:
    await web_login(hub, user_id)
    return await register_client(hub)


def _resource(alias: str) -> str:
    return f"{PUBLIC_URL}/mcp/{alias}"


async def _authorize(
    hub: Hub,
    client_id: str,
    *,
    scope: str | None,
    alias: str = "tag",
    resource: str | None = None,
    state: str | None = "state-1",
    challenge: str | None = None,
) -> Any:
    params = authorize_params(
        client_id,
        challenge=challenge if challenge is not None else pkce_pair()[1],
        state=state,
        resource=_resource(alias) if resource is None else (resource or None),
        scope=scope,
    )
    if resource == "":
        params.pop("resource", None)
    return await hub.get("/oauth/authorize", params=params)


def _error_of(response: Any) -> str | None:
    """Код ошибки, если ответ — редирект клиенту на его ``redirect_uri`` (R-O4.2)."""
    if response.status_code != 302:
        return None
    location = response.headers["location"]
    if not location.startswith(LOOPBACK_REDIRECT):
        return None
    return query_of(location).get("error")


CHECKED_RE = re.compile(r'<input type="radio" name="preset" value="(\w+)"[^>]*checked')


def _checked_preset(html: str) -> str:
    match = CHECKED_RE.search(html)
    assert match, f"на экране прав не отмечен ни один пресет: {html}"
    return match.group(1)


def _provider_scope(location: str) -> set[str]:
    """Набор значений ``scope``, ушедший в целевую систему в запросе authorize (R-B2)."""
    return set(query_of(location)["scope"].split())


def _provider_redirect(response: Any, alias: str) -> str:
    """Редирект на authorize целевой системы (R-B2); иначе — понятная диагностика."""
    assert response.status_code == 302, response.text
    location = str(response.headers["location"])
    assert location.startswith(f"{_as_base(alias)}/oauth/authorize"), location
    return location


def _as_base(alias: str) -> str:
    return {"tag": TAG_AS, "jira": JIRA_AS, "confluence": CONFLUENCE_AS}[alias]


async def _connection_rows(hub: Hub, user_id: str = "u1") -> list[dict[str, Any]]:
    return await fetch_rows(
        hub.app,
        "SELECT alias, status, preset, groups FROM connections WHERE user_id = :u",
        u=user_id,
    )


# --- AC-203 ----------------------------------------------------------------


@pytest.mark.ac("AC-203")
async def test_both_scopes_pass_authorize_and_give_readonly_preset(
    make_hub: HubFactory, net: MockNetwork
) -> None:
    """Штатный путь MCP-клиента: обе объявленные области → подключение с пресетом readonly."""
    hub = await _hub(make_hub, net, consent="always")
    client_id = await _ready_client(hub)
    verifier, challenge = pkce_pair()

    started = await _authorize(
        hub, client_id, scope="tag:readonly tag:readwrite", alias="tag", challenge=challenge
    )
    assert _error_of(started) is None, started.headers.get("location")
    location = _provider_redirect(started, "tag")
    # В целевую систему ушли права пресета readonly и ни одного значения readwrite (R-O5.2).
    assert _provider_scope(location) == set(PROVIDER_SCOPES["tag"]["readonly"])
    assert _provider_scope(location) & set(PROVIDER_SCOPES["tag"]["readwrite"]) == set()

    page = await provider_callback(hub, location, alias="tag")
    assert page.status_code == 200, page.text
    assert "tag:readonly tag:readwrite" in page.text
    assert "все объявленные области" in page.text
    assert "выбира" in page.text
    assert _checked_preset(page.text) == "readonly"

    allowed = await submit_consent(hub, page.text, preset="readonly", groups=["read_messages"])
    assert allowed.status_code == 302, allowed.text
    assert _error_of(allowed) is None
    code = query_of(allowed.headers["location"])["code"]

    tokens = await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)
    assert tokens.status_code == 200, tokens.text
    assert tokens.json()["scope"] == "tag:readonly"

    rows = await _connection_rows(hub)
    assert [(r["alias"], r["preset"]) for r in rows] == [("tag", "readonly")]


@pytest.mark.ac("AC-203")
async def test_single_scope_page_has_no_set_explanation(
    make_hub: HubFactory, net: MockNetwork
) -> None:
    """Пояснение про набор областей появляется только при двух и более элементах (R-W3)."""
    hub = await _hub(make_hub, net, consent="always")
    client_id = await _ready_client(hub)
    started = await _authorize(hub, client_id, scope="tag:readonly", alias="tag")
    page = await provider_callback(hub, _provider_redirect(started, "tag"), alias="tag")
    assert page.status_code == 200, page.text
    assert "tag:readonly" in page.text
    assert "все объявленные области" not in page.text


# --- AC-204 ----------------------------------------------------------------


def _scope_forms(alias: str) -> list[tuple[str, str]]:
    return [
        ("прямой порядок", f"{alias}:readonly {alias}:readwrite"),
        ("обратный порядок", f"{alias}:readwrite {alias}:readonly"),
        ("два пробела", f"{alias}:readonly  {alias}:readwrite"),
        ("окружающие пробелы", f"  {alias}:readonly {alias}:readwrite  "),
        ("дубль", f"{alias}:readonly {alias}:readonly {alias}:readwrite"),
    ]


@pytest.mark.ac("AC-204")
@pytest.mark.parametrize("alias", ALIASES)
@pytest.mark.parametrize(
    ("title", "scope"),
    _scope_forms("{alias}"),
    ids=[case[0] for case in _scope_forms("x")],
)
async def test_order_repeats_and_spaces_do_not_matter(
    make_hub: HubFactory, net: MockNetwork, alias: str, title: str, scope: str
) -> None:
    """Порядок, повторы и лишние пробелы не влияют: набор канонизируется (R-O5.1, R-W3)."""
    hub = await _hub(make_hub, net, consent="always")
    client_id = await _ready_client(hub)
    canonical = f"{alias}:readonly {alias}:readwrite"

    started = await _authorize(hub, client_id, scope=scope.format(alias=alias), alias=alias)
    assert _error_of(started) is None, f"{title}: {started.headers.get('location')}"
    page = await provider_callback(hub, _provider_redirect(started, alias), alias=alias)
    assert page.status_code == 200, page.text
    assert canonical in page.text, title

    allowed = await submit_consent(hub, page.text, preset="readonly", groups=["read_messages"])
    assert allowed.status_code == 302, allowed.text
    rows = await fetch_rows(hub.app, "SELECT alias, scope FROM consents")
    assert rows == [{"alias": alias, "scope": canonical}], title


# --- AC-205 ----------------------------------------------------------------


def _non_empty_subsets(values: list[str]) -> list[str]:
    """Все непустые подмножества набора — в прямом и в обратном порядке элементов."""
    result: list[str] = []
    for size in range(1, len(values) + 1):
        for combo in combinations(values, size):
            result.append(" ".join(combo))
            if size > 1:
                result.append(" ".join(reversed(combo)))
    return result


@pytest.mark.ac("AC-205")
@pytest.mark.parametrize("alias", ALIASES)
async def test_declared_scopes_are_accepted_by_authorize(
    make_hub: HubFactory, net: MockNetwork, alias: str
) -> None:
    """Инвариант R-O5.1: объявленное в метаданных принимается целиком и любым подмножеством.

    Набор берётся из самих метаданных Hub, а не из захардкоженных строк: правило сформулировано
    как согласованность ``scopes_supported`` и проверки на ``/oauth/authorize``, поэтому тест
    обязан перебрать всё, что Hub объявил.
    """
    hub = await _hub(make_hub, net, consent="always")
    client_id = await _ready_client(hub)

    prm = await hub.get(f"/.well-known/oauth-protected-resource/mcp/{alias}")
    assert prm.status_code == 200, prm.text
    declared = list(prm.json()["scopes_supported"])
    assert declared, f"{alias}: метаданные ресурса не объявляют ни одной области"

    as_metadata = await hub.get("/.well-known/oauth-authorization-server")
    assert as_metadata.status_code == 200, as_metadata.text
    from_as = [s for s in as_metadata.json()["scopes_supported"] if s.split(":")[0] == alias]
    assert from_as, f"{alias}: метаданные сервера авторизации не объявляют областей коннектора"

    checked = 0
    for source in (declared, from_as):
        for scope in _non_empty_subsets(source):
            response = await _authorize(hub, client_id, scope=scope, alias=alias)
            assert _error_of(response) != "invalid_scope", (
                f"{alias}: объявленный набор {scope!r} отвергнут "
                f"({response.headers.get('location')})"
            )
            assert _error_of(response) is None, f"{alias}: {scope!r} → {_error_of(response)}"
            checked += 1
    assert checked == 2 * len(_non_empty_subsets(declared))
    assert checked >= 8, f"{alias}: перебрано слишком мало наборов ({checked})"


# --- AC-206 ----------------------------------------------------------------


_INVALID_SCOPES = [
    ("разные коннекторы", "tag:readonly jira:readonly"),
    ("неизвестный суффикс", "tag:readonly tag:admin"),
    ("неизвестный alias", "tag:readonly nope:readonly"),
    ("пустой суффикс", "tag:readonly tag:"),
    ("пустой alias", "tag:readonly :readwrite"),
    ("без двоеточия", "tag:readonly readwrite"),
    ("три элемента, один чужой", "tag:readwrite tag:readonly jira:readwrite"),
]


@pytest.mark.ac("AC-206")
@pytest.mark.parametrize(
    ("title", "scope"), _INVALID_SCOPES, ids=[case[0] for case in _INVALID_SCOPES]
)
async def test_invalid_element_of_set_is_invalid_scope(
    make_hub: HubFactory, net: MockNetwork, title: str, scope: str
) -> None:
    """Недопустимый элемент набора и элементы разных коннекторов — ``invalid_scope`` (R-O5.1)."""
    hub = await _hub(make_hub, net, consent="always")
    client_id = await _ready_client(hub)
    state = "xyz 1"

    response = await _authorize(hub, client_id, scope=scope, resource="", state=state)
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(LOOPBACK_REDIRECT), location
    query = query_of(location)
    assert query["error"] == "invalid_scope", title
    assert query["state"] == state
    assert "code" not in query

    assert "oauthtx:" not in dump_kv(hub.app)
    assert await _connection_rows(hub) == []
    assert await fetch_rows(hub.app, "SELECT id FROM oauth_codes") == []


@pytest.mark.ac("AC-206")
async def test_scope_and_resource_mismatch_is_invalid_request(
    make_hub: HubFactory, net: MockNetwork
) -> None:
    """Расхождение ``scope`` и ``resource`` — по-прежнему ``invalid_request`` (R-O5)."""
    hub = await _hub(make_hub, net, consent="always")
    client_id = await _ready_client(hub)
    state = "xyz 1"

    response = await _authorize(
        hub, client_id, scope="tag:readonly", resource=_resource("jira"), state=state
    )
    assert response.status_code == 302, response.text
    query = query_of(response.headers["location"])
    assert query["error"] == "invalid_request"
    assert query["state"] == state
    assert "code" not in query
    assert "oauthtx:" not in dump_kv(hub.app)


# --- AC-207 ----------------------------------------------------------------


@pytest.mark.ac("AC-207")
@pytest.mark.parametrize(
    ("title", "scope"),
    [("пустое значение", ""), ("одни пробелы", "   "), ("параметра нет", None)],
    ids=["пустое значение", "одни пробелы", "параметра нет"],
)
async def test_empty_scope_equals_absent_parameter(
    make_hub: HubFactory, net: MockNetwork, title: str, scope: str | None
) -> None:
    """Пустое и пробельное значение равнозначны отсутствию параметра → ``tag:readonly`` (R-O5.1)."""
    hub = await _hub(make_hub, net, consent="always")
    client_id = await _ready_client(hub)
    verifier, challenge = pkce_pair()

    started = await _authorize(hub, client_id, scope=scope, alias="tag", challenge=challenge)
    assert _error_of(started) is None, f"{title}: {started.headers.get('location')}"
    page = await provider_callback(hub, _provider_redirect(started, "tag"), alias="tag")
    assert page.status_code == 200, page.text
    allowed = await submit_consent(hub, page.text, preset="readonly", groups=["read_messages"])
    code = query_of(allowed.headers["location"])["code"]
    tokens = await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)
    assert tokens.status_code == 200, tokens.text
    assert tokens.json()["scope"] == "tag:readonly", title


# --- AC-208 ----------------------------------------------------------------


@pytest.mark.ac("AC-208")
@pytest.mark.parametrize("preset", ["readwrite", "readonly"])
async def test_single_scope_keeps_previous_behaviour(
    make_hub: HubFactory, net: MockNetwork, preset: str
) -> None:
    """Запрос ровно одной области ведёт себя как прежде (R-O5.2, совместимость с AC-84/AC-85)."""
    hub = await _hub(make_hub, net, consent="always")
    client_id = await _ready_client(hub)
    verifier, challenge = pkce_pair()
    groups = ["post_messages"] if preset == "readwrite" else ["read_messages"]

    started = await _authorize(
        hub, client_id, scope=f"tag:{preset}", alias="tag", challenge=challenge
    )
    location = _provider_redirect(started, "tag")
    assert _provider_scope(location) == set(PROVIDER_SCOPES["tag"][preset])

    page = await provider_callback(hub, location, alias="tag")
    assert page.status_code == 200, page.text
    assert _checked_preset(page.text) == preset

    allowed = await submit_consent(hub, page.text, preset=preset, groups=groups)
    assert allowed.status_code == 302, allowed.text
    code = query_of(allowed.headers["location"])["code"]
    tokens = await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)
    assert tokens.status_code == 200, tokens.text
    assert tokens.json()["scope"] == f"tag:{preset}"


# --- AC-209 ----------------------------------------------------------------


@pytest.mark.ac("AC-209")
async def test_preset_of_existing_connection_wins_for_both_scopes(
    make_hub: HubFactory, net: MockNetwork
) -> None:
    """При запросе обеих областей пресет берётся из подключения (R-O5.2, R-O6.2)."""
    hub = await _hub(make_hub, net, consent="always")
    client_id = await _ready_client(hub)
    verifier, challenge = pkce_pair()
    await seed_connection(
        hub, alias="tag", preset="readwrite", groups=("post_messages",), status="connected"
    )
    provider = net.providers["tag"]

    page = await _authorize(
        hub, client_id, scope="tag:readonly tag:readwrite", alias="tag", challenge=challenge
    )
    assert page.status_code == 200, page.text  # подключение есть — OAuth системы не запускался
    assert _checked_preset(page.text) == "readwrite"

    allowed = await submit_consent(hub, page.text, preset="readwrite", groups=["post_messages"])
    assert allowed.status_code == 302, allowed.text
    assert allowed.headers["location"].startswith(LOOPBACK_REDIRECT), allowed.headers["location"]
    code = query_of(allowed.headers["location"])["code"]
    tokens = await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)
    assert tokens.status_code == 200, tokens.text
    assert tokens.json()["scope"] == "tag:readwrite"

    assert provider.token_requests == [], "повторный OAuth целевой системы не требовался"
    rows = await _connection_rows(hub)
    assert [(r["alias"], r["preset"]) for r in rows] == [("tag", "readwrite")]


# --- AC-210 ----------------------------------------------------------------


@pytest.mark.ac("AC-210")
async def test_both_scopes_do_not_silently_upgrade_rights(
    make_hub: HubFactory, net: MockNetwork
) -> None:
    """Запрос обеих областей не повышает права молча; повышение — только через R-B7 (AC-111)."""
    hub = await _hub(make_hub, net, consent="always")
    client_id = await _ready_client(hub)
    verifier, challenge = pkce_pair()
    await seed_connection(
        hub, alias="tag", preset="readonly", groups=("read_messages",), status="connected"
    )
    provider = net.providers["tag"]

    page = await _authorize(
        hub, client_id, scope="tag:readonly tag:readwrite", alias="tag", challenge=challenge
    )
    assert page.status_code == 200, page.text
    assert _checked_preset(page.text) == "readonly"

    allowed = await submit_consent(hub, page.text, preset="readonly", groups=["read_messages"])
    assert allowed.status_code == 302, allowed.text
    assert allowed.headers["location"].startswith(LOOPBACK_REDIRECT), allowed.headers["location"]
    code = query_of(allowed.headers["location"])["code"]
    tokens = await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)
    assert tokens.json()["scope"] == "tag:readonly"
    assert provider.token_requests == []
    rows = await _connection_rows(hub)
    assert [(r["alias"], r["preset"]) for r in rows] == [("tag", "readonly")]

    # Тот же набор, но пользователь сам выбирает «Чтение и запись» — повторный OAuth системы.
    verifier2, challenge2 = pkce_pair("verifier-two-0123456789abcdefghij")
    again = await _authorize(
        hub, client_id, scope="tag:readonly tag:readwrite", alias="tag", challenge=challenge2
    )
    assert again.status_code == 200, again.text
    upgraded = await submit_consent(hub, again.text, preset="readwrite", groups=["post_messages"])
    assert upgraded.status_code == 302, upgraded.text
    location = upgraded.headers["location"]
    assert location.startswith(f"{TAG_AS}/oauth/authorize"), location
    assert _provider_scope(location) == set(PROVIDER_SCOPES["tag"]["readwrite"])

    finished = await provider_callback(hub, location, alias="tag", code="prov-code-2")
    assert finished.status_code == 200, finished.text
    confirmed = await submit_consent(hub, finished.text, preset="readwrite", groups=["post_messages"])
    assert confirmed.status_code == 302, confirmed.text
    code2 = query_of(confirmed.headers["location"])["code"]
    tokens2 = await exchange_code(hub, code=code2, client_id=client_id, verifier=verifier2)
    assert tokens2.status_code == 200, tokens2.text
    assert tokens2.json()["scope"] == "tag:readwrite"
    assert len(provider.token_requests) == 1


# --- AC-211 ----------------------------------------------------------------


@pytest.mark.ac("AC-211")
async def test_remembered_consent_matches_by_set(make_hub: HubFactory, net: MockNetwork) -> None:
    """Сохранённое согласие сопоставляется по набору областей, а не по строке (R-O6.3)."""
    hub = await _hub(make_hub, net, consent="remember")
    client_id = await _ready_client(hub)
    verifier, challenge = pkce_pair()

    started = await _authorize(
        hub, client_id, scope="tag:readonly tag:readwrite", alias="tag", challenge=challenge
    )
    page = await provider_callback(hub, _provider_redirect(started, "tag"), alias="tag")
    assert page.status_code == 200, page.text
    upgraded = await submit_consent(hub, page.text, preset="readwrite", groups=["post_messages"])
    # readonly → readwrite требует повторного OAuth целевой системы (R-B7).
    assert upgraded.status_code == 302, upgraded.text
    finished = await provider_callback(
        hub, upgraded.headers["location"], alias="tag", code="prov-code-2"
    )
    if finished.status_code == 200:
        finished = await submit_consent(
            hub, finished.text, preset="readwrite", groups=["post_messages"]
        )
    assert finished.status_code == 302, finished.text
    first_code = query_of(finished.headers["location"])["code"]
    assert (
        await exchange_code(hub, code=first_code, client_id=client_id, verifier=verifier)
    ).json()["scope"] == "tag:readwrite"

    equivalents = (
        ("обратный порядок, два пробела", "tag:readwrite  tag:readonly", "verifier-reverse"),
        ("дубль", "tag:readonly tag:readonly tag:readwrite", "verifier-duplicate"),
    )
    for title, scope, seed in equivalents:
        verifier_n, challenge_n = pkce_pair(f"{seed}-0123456789abcdef")
        response = await _authorize(
            hub, client_id, scope=scope, alias="tag", challenge=challenge_n
        )
        assert response.status_code == 302, f"{title}: экран прав показан снова ({response.text})"
        assert _error_of(response) is None, title
        query = query_of(response.headers["location"])
        assert "code" in query, title
        tokens = await exchange_code(
            hub, code=query["code"], client_id=client_id, verifier=verifier_n
        )
        assert tokens.status_code == 200, tokens.text
        assert tokens.json()["scope"] == "tag:readwrite", title

    other = await _authorize(hub, client_id, scope="tag:readonly", alias="tag")
    assert other.status_code == 200, other.text  # другой набор — экран прав показывается
    assert _error_of(other) is None


# --- AC-212 ----------------------------------------------------------------


_REFRESH_CASES: list[tuple[str, str, str | None, int, str | None]] = [
    ("а: дубль", "readonly", "tag:readonly tag:readonly", 200, "tag:readonly"),
    ("б: расширение", "readonly", "tag:readonly tag:readwrite", 400, None),
    ("в: пробелы", "readwrite", "  tag:readonly  ", 200, "tag:readonly"),
    ("г: обратный порядок", "readwrite", "tag:readwrite tag:readonly", 200, "tag:readwrite"),
    ("д: чужой alias", "readonly", "jira:readonly", 400, None),
    ("е: без scope", "readonly", None, 200, "tag:readonly"),
]


@pytest.mark.ac("AC-212")
@pytest.mark.parametrize(
    ("title", "granted", "scope", "status", "expected"),
    _REFRESH_CASES,
    ids=[case[0] for case in _REFRESH_CASES],
)
async def test_refresh_parses_scope_as_set_and_forbids_widening(
    make_hub: HubFactory,
    net: MockNetwork,
    title: str,
    granted: str,
    scope: str | None,
    status: int,
    expected: str | None,
) -> None:
    """Обновление токена разбирает ``scope`` множеством и запрещает расширение (R-O10)."""
    hub = await _hub(make_hub, net)
    _conn, tokens = await connected_client(
        hub, alias="tag", preset=granted, groups=("read_messages",)
    )
    extra = {} if scope is None else {"scope": scope}
    response = await refresh_grant(
        hub, refresh_token=tokens["refresh_token"], client_id=tokens["client_id"], **extra
    )
    assert response.status_code == status, response.text

    rows = await fetch_rows(hub.app, "SELECT token_sha256, status, scope FROM refresh_tokens")
    if status == 200:
        assert response.json()["scope"] == expected, title
        assert len(rows) == 2, title
    else:
        assert response.json()["error"] == "invalid_scope", title
        assert len(rows) == 1, f"{title}: выдан новый refresh-токен"
        assert rows[0]["status"] == "active", title
        assert rows[0]["scope"] == f"tag:{granted}", title


# --- AC-213 ----------------------------------------------------------------


async def _connect_via_authorize(
    hub: Hub, *, user_id: str, key: str, scope: str, verifier_seed: str
) -> dict[str, Any]:
    """Подключение штатным путём: authorize → OAuth системы → экран прав → обмен кода."""
    await add_key(hub, key, user_id)
    client_id = await _ready_client(hub, user_id)
    verifier, challenge = pkce_pair(verifier_seed)
    started = await _authorize(hub, client_id, scope=scope, alias="tag", challenge=challenge)
    page = await provider_callback(hub, _provider_redirect(started, "tag"), alias="tag")
    assert page.status_code == 200, page.text
    allowed = await submit_consent(hub, page.text, preset="readonly", groups=["read_messages"])
    assert allowed.status_code == 302, allowed.text
    code = query_of(allowed.headers["location"])["code"]
    tokens = await exchange_code(hub, code=code, client_id=client_id, verifier=verifier)
    assert tokens.status_code == 200, tokens.text
    return dict(tokens.json())


@pytest.mark.ac("AC-213")
async def test_scope_form_does_not_change_actual_rights(
    make_hub: HubFactory, net: MockNetwork
) -> None:
    """Форма запрошенного scope не влияет на фактические права подключения (R-O5.2)."""
    hub = await _hub(make_hub, net, consent="always")

    first = await _connect_via_authorize(
        hub,
        user_id="u1",
        key="sk-u1",
        scope="tag:readonly tag:readwrite",
        verifier_seed="verifier-user-one-0123456789ab",
    )
    web_logout(hub)
    second = await _connect_via_authorize(
        hub,
        user_id="u2",
        key="sk-u2",
        scope="tag:readonly",
        verifier_seed="verifier-user-two-0123456789ab",
    )

    listings = []
    tool_lists = []
    calls = []
    for key, tokens in (("sk-u1", first), ("sk-u2", second)):
        listed = await hub.get("/api/me/connections", headers=bearer(key))
        assert listed.status_code == 200, listed.text
        connection = next(c for c in listed.json() if c["alias"] == "tag")
        assert "scope" not in connection, connection
        listings.append(connection["preset"])

        tools = await hub.post(
            "/mcp/tag", content=jsonrpc_body("tools/list"), headers=mcp_headers(tokens["access_token"])
        )
        assert tools.status_code == 200, tools.text
        tool_lists.append([t["name"] for t in tools.json()["result"]["tools"]])

        called = await hub.post(
            "/mcp/tag",
            content=jsonrpc_body("tools/call", {"name": "create_post", "arguments": {}}),
            headers=mcp_headers(tokens["access_token"]),
        )
        assert called.status_code == 200, called.text
        calls.append(called.json()["error"]["code"])

    assert listings == ["readonly", "readonly"]
    assert tool_lists[0] == tool_lists[1]
    assert "create_post" not in tool_lists[0]
    assert calls == [CODE_TOOL_FORBIDDEN, CODE_TOOL_FORBIDDEN]
