"""Выход из приложения и отзыв ключа LiteLLM (R-L11, R-L12): AC-244…AC-249.

Ключ выпускается правами самого пользователя, и его значение Hub не хранит (R-L5). Отсюда всё
устройство ревизии: отозвать ключ по значению можно ровно в том запросе, где пользователь его
предъявил, а прежние ключи — только по алиасу и только в момент входа, пока под рукой SSO-JWT.
Проверяется наблюдаемое: что ушло в LiteLLM, что осталось в ``api_keys``, чем ключ отвечает сразу
после выхода и что записано в аудит.

Все проверки идут против локальных моков; обращений в сеть нет.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.conftest import Hub, HubFactory
from tests.support import (
    add_key,
    audit_rows,
    bearer,
    capture_all_levels,
    capture_json_logs,
    dump_kv,
    fetch_rows,
    hub_log,
    insert_key,
    key_delete_calls,
    key_delete_credentials,
    litellm_paths,
    make_jwt,
    mock_key_delete,
    mock_key_generate,
    mock_poll,
    mock_start,
    ready_body,
    record_text,
    seed_user_with_key,
    sha256_hex,
    start_body,
)

LOGOUT = "/api/me/key"


# --- вспомогательное -------------------------------------------------------


async def _api_keys(hub: Hub, user_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT user_id, key_sha256, key_alias, key_kind FROM api_keys"
    if user_id is not None:
        return await fetch_rows(hub.app, sql + " WHERE user_id = :u ORDER BY id", u=user_id)
    return await fetch_rows(hub.app, sql + " ORDER BY id")


async def _logout(hub: Hub, key: str) -> httpx.Response:
    return await hub.client.delete(LOGOUT, headers=bearer(key))


async def _warm_key_cache(hub: Hub, key: str) -> None:
    """Использовать ключ, чтобы положительный результат лёг в кэш ``keyauth`` (R-L6)."""
    response = await hub.get("/api/me", headers=bearer(key))
    assert response.status_code == 200, response.text
    assert await hub.app.state.kv.get(f"keyauth:{sha256_hex(key)}") is not None


async def _login(
    hub: Hub, key: str, *, ll_id: str = "ll-1", user_id: str = "u1"
) -> str:
    """Полный вход через CLI-SSO; возвращает SSO-JWT, которым выполнялся ``/key/generate``."""
    mock_start(hub.litellm, start_body(login_id=ll_id))
    start = await hub.post("/cli/start", json={"client": "opencode-fork/1.17.9"})
    assert start.status_code == 200, start.text
    body = start.json()
    jwt = make_jwt({"sub": user_id, "email": f"{user_id}@corp.test",
                    "exp": int(hub.clock.time()) + 3600})
    mock_poll(hub.litellm, ready_body(jwt, user_id=user_id, team_id="t1"), login_id=ll_id)
    mock_key_generate(hub.litellm, key)
    polled = await hub.poll(body["login_id"], body["poll_secret"])
    assert polled.status_code == 200, polled.text
    assert polled.json()["key"] == key
    return jwt


# --- AC-244: выход отзывает, убирает и обесценивает немедленно --------------


@pytest.mark.ac("AC-244")
async def test_logout_revokes_cleans_up_and_invalidates_immediately(
    hub: Hub, caplog: pytest.LogCaptureFixture
) -> None:
    """Порядок отзыв → уборка → аудит и немедленная недействительность ключа (R-L11)."""
    await seed_user_with_key(hub.app, "sk-out-1", user_id="u1")
    await _warm_key_cache(hub, "sk-out-1")
    route = mock_key_delete(hub.litellm)
    capture_all_levels(caplog)

    with capture_json_logs() as json_logs:
        response = await _logout(hub, "sk-out-1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["revoked"] is True
    assert body["revoke_error"] is None
    assert isinstance(body["message"], str) and body["message"].strip()
    assert "ключ" in body["message"].lower()

    # Отзыв идёт по значению предъявленного ключа и ровно один раз.
    assert route.call_count == 1
    assert key_delete_calls(hub.litellm) == [{"keys": ["sk-out-1"]}]
    # Служебный ключ не задан — самоотзыв значением самого ключа (R-L11.4б).
    assert key_delete_credentials(hub.litellm) == ["Bearer sk-out-1"]

    # Локальная уборка: строки ключа нет, строка пользователя сохранена.
    assert await _api_keys(hub) == []
    assert len(await fetch_rows(hub.app, "SELECT user_id FROM users")) == 1

    # Немедленно, без единого сдвига часов: кэш keyauth сброшен (R-L11.5).
    assert await hub.app.state.kv.get(f"keyauth:{sha256_hex('sk-out-1')}") is None
    assert (await hub.get("/api/me", headers=bearer("sk-out-1"))).status_code == 401

    logout_rows = await audit_rows(hub.app, "logout")
    assert len(logout_rows) == 1
    assert logout_rows[0]["details"] == {
        "key_kind": "persistent",
        "key_alias": "opencode-u1-20260101-1200",
        "revoked": True,
        "revoke_error": None,
    }
    revoked_rows = await audit_rows(hub.app, "key_revoked")
    assert len(revoked_rows) == 1
    assert revoked_rows[0]["details"]["reason"] == "logout"
    assert revoked_rows[0]["details"]["outcome"] == "ok"

    # R-L11.7: повторный вызов — 401, второго запроса и новых записей аудита нет.
    again = await _logout(hub, "sk-out-1")
    assert again.status_code == 401, again.text
    assert route.call_count == 1, "ушёл второй запрос отзыва"
    assert len(await audit_rows(hub.app, "logout")) == 1
    assert len(await audit_rows(hub.app, "key_revoked")) == 1
    assert await _api_keys(hub) == []
    assert hub_log(caplog, json_logs), "журнал пуст — проверка вырождена"


@pytest.mark.ac("AC-244")
async def test_revocation_happens_before_local_cleanup(hub: Hub) -> None:
    """Отзыв идёт до уборки: после ответа повторить его будет нечем (R-L11.3).

    Наблюдаемо это так: в момент запроса отзыва строка ключа ещё на месте — если бы уборка шла
    первой, значение ключа было бы уже недоступно обработчику как учётные данные.
    """
    await seed_user_with_key(hub.app, "sk-order", user_id="u1")
    await _warm_key_cache(hub, "sk-order")
    cache_key = f"keyauth:{sha256_hex('sk-order')}"
    seen: list[bool] = []

    def _record(request: httpx.Request) -> httpx.Response:
        # Уборка сбрасывает кэш аутентификации; на момент отзыва он обязан быть ещё на месте.
        seen.append(cache_key in dump_kv(hub.app))
        return httpx.Response(200, json={"deleted_keys": ["sk-order"]})

    mock_key_delete(hub.litellm, side_effect=_record)
    assert (await _logout(hub, "sk-order")).status_code == 200
    assert seen == [True], "уборка выполнена раньше отзыва"
    assert await _api_keys(hub) == []
    assert cache_key not in dump_kv(hub.app)


# --- AC-245: закрытая таблица исходов отзыва -------------------------------


_OUTCOMES: list[tuple[str, Any, bool, str | None]] = [
    ("404 — отзывать нечего", httpx.Response(404, json={"detail": "not found"}), True, None),
    ("401", httpx.Response(401, json={"detail": "no"}), False, "not_permitted"),
    ("403", httpx.Response(403, json={"detail": "no"}), False, "not_permitted"),
    ("429", httpx.Response(429, json={"detail": "slow"}), False, "upstream_unavailable"),
    ("500", httpx.Response(500, json={"detail": "boom"}), False, "upstream_unavailable"),
    ("сетевая ошибка", httpx.ConnectError("refused"), False, "upstream_unavailable"),
    ("таймаут", httpx.ReadTimeout("timed out"), False, "upstream_unavailable"),
    ("не JSON", "не json вовсе", False, "invalid_response"),
    ("418", httpx.Response(418, json={"detail": "teapot"}), False, "invalid_response"),
    # R-L11.4: 2xx с телом, не являющимся JSON-объектом, — тоже invalid_response (fix a10508d);
    # раньше body is not None засчитывало [], 0, строку и null успешным отзывом.
    ("2xx тело — пустой список", httpx.Response(200, json=[]), False, "invalid_response"),
    ("2xx тело — число", httpx.Response(200, json=0), False, "invalid_response"),
    ("2xx тело — строка (валидный JSON)", httpx.Response(200, json="ok"), False, "invalid_response"),
    ("2xx тело — null", httpx.Response(200, json=None), False, "invalid_response"),
]


@pytest.mark.ac("AC-245")
@pytest.mark.parametrize(
    ("title", "outcome", "revoked", "revoke_error"), _OUTCOMES, ids=[c[0] for c in _OUTCOMES]
)
async def test_revocation_failure_never_cancels_the_logout(
    hub: Hub, title: str, outcome: Any, revoked: bool, revoke_error: str | None
) -> None:
    """Отказ LiteLLM выход не отменяет, но виден кодом причины (R-L11.4, R-L11.6, R-L11.7)."""
    await seed_user_with_key(hub.app, "sk-out-2", user_id="u1")
    await _warm_key_cache(hub, "sk-out-2")
    if isinstance(outcome, str):
        mock_key_delete(hub.litellm, body=outcome)
    elif isinstance(outcome, httpx.Response):
        mock_key_delete(hub.litellm, side_effect=[outcome])
    else:
        mock_key_delete(hub.litellm, side_effect=outcome)

    response = await _logout(hub, "sk-out-2")
    assert response.status_code == 200, f"{title}: {response.text}"
    body = response.json()
    assert body["status"] == "ok", title
    assert body["revoked"] is revoked, title
    assert body["revoke_error"] == revoke_error, title
    # message объясняет исход по-русски и при неудаче не молчит о ней (решение 112).
    assert isinstance(body["message"], str) and body["message"].strip(), title
    if revoke_error is not None:
        assert "не удалось" in body["message"], title

    # Локальная уборка выполняется всегда, каким бы ни был исход отзыва (R-L11.3).
    assert await _api_keys(hub) == [], title
    assert (await hub.get("/api/me", headers=bearer("sk-out-2"))).status_code == 401, title

    logout_rows = await audit_rows(hub.app, "logout")
    assert [r["details"]["revoke_error"] for r in logout_rows] == [revoke_error], title
    assert [r["details"]["revoked"] for r in logout_rows] == [revoked], title
    revoked_rows = await audit_rows(hub.app, "key_revoked")
    assert [r["details"]["outcome"] for r in revoked_rows] == ["ok" if revoked else "failed"], title
    assert revoked_rows[0]["details"]["reason"] == "logout", title


@pytest.mark.ac("AC-245")
def test_all_revoke_error_codes_are_from_the_closed_set() -> None:
    """Набор кодов причины закрыт и совпадает с тем, что реально возвращает ``revoke_error_for``.

    В отличие от прежней версии (ревью, финдинг 3: тест не касался ``src/`` и не мог упасть ни при
    какой поломке), утверждения строятся о РЕАЛЬНОЙ функции ``hub.litellm.revoke_error_for`` и о
    константах, импортированных из неё же, а не о собственной таблице модуля теста. Ломается на:
    смене значения константы, потере ветки (404, 401/403, 429/500, 2xx-не-объект, сеть/None),
    неверном соответствии код → причина.
    """
    from hub.litellm import (
        REVOKE_INVALID_RESPONSE,
        REVOKE_NOT_PERMITTED,
        REVOKE_UPSTREAM_UNAVAILABLE,
        revoke_error_for,
    )

    allowed = {None, REVOKE_NOT_PERMITTED, REVOKE_UPSTREAM_UNAVAILABLE, REVOKE_INVALID_RESPONSE}
    # (status, body, ожидаемая причина) — независимо вычислено по R-L11.4, а не списано из revoke_error_for.
    cases: list[tuple[int | None, Any, str | None]] = [
        (None, None, REVOKE_UPSTREAM_UNAVAILABLE),  # сеть/таймаут
        (404, {"detail": "not found"}, None),  # отзывать нечего — успех
        (401, {"detail": "no"}, REVOKE_NOT_PERMITTED),
        (403, {"detail": "no"}, REVOKE_NOT_PERMITTED),
        (429, {"detail": "slow"}, REVOKE_UPSTREAM_UNAVAILABLE),
        (500, {"detail": "boom"}, REVOKE_UPSTREAM_UNAVAILABLE),
        (200, {"deleted_keys": []}, None),  # JSON-объект — успех
        (200, None, REVOKE_INVALID_RESPONSE),  # не JSON вовсе
        (200, [], REVOKE_INVALID_RESPONSE),  # 2xx, но не объект (fix a10508d)
        (200, 0, REVOKE_INVALID_RESPONSE),
        (200, "ok", REVOKE_INVALID_RESPONSE),
        (418, {"detail": "teapot"}, REVOKE_INVALID_RESPONSE),  # прочий 4xx
    ]

    produced: set[str | None] = set()
    for status, body, expected in cases:
        actual = revoke_error_for(status, body)
        assert actual == expected, (status, body)
        produced.add(actual)

    assert produced <= allowed
    # И каждое значение набора действительно достижимо — иначе таблица проверена не целиком.
    assert produced == allowed


# --- AC-246: выход трогает только ключ -------------------------------------


@pytest.mark.ac("AC-246")
async def test_logout_touches_the_key_and_nothing_else(make_hub: HubFactory) -> None:
    """Подключения, токены целевых систем, веб-сессии и чужие ключи не затронуты (R-L11.8)."""
    from tests.support import (
        TAG_ENV,
        catalog_doc,
        connect_with_token,
        issue_hub_tokens,
        jsonrpc_body,
        mcp_headers,
        user_token_facade,
        web_login,
    )

    hub = await make_hub(
        catalog=catalog_doc([user_token_facade("tag")]),
        env=TAG_ENV,
        base_url="https://hub.test",
        login_revokes_previous_keys=False,
    )
    assert hub.net is not None
    await seed_user_with_key(hub.app, "sk-a", user_id="u1")
    await insert_key(hub.app, "sk-b", "u1", key_alias="opencode-u1-20260101-1300")
    await seed_user_with_key(hub.app, "sk-u2", user_id="u2", email="u2@corp.test")
    await web_login(hub, "u1")

    connected = await connect_with_token(hub, alias="tag", token="SESSION-1", key="sk-a")
    assert connected.status_code == 200, connected.text
    connection = await hub.app.state.broker.load_connection("u1", "tag")
    tokens = await issue_hub_tokens(
        hub, user_id="u1", alias="tag", connection_id=connection.id, scope="tag:readonly"
    )
    before_conn = await fetch_rows(hub.app, "SELECT * FROM connections WHERE user_id = 'u1'")
    before_upstream = await fetch_rows(hub.app, "SELECT * FROM upstream_tokens")
    verify_calls = hub.net.verify.calls
    revoke_calls = len(hub.net.tokens.revoke_requests)

    route = mock_key_delete(hub.litellm)
    assert (await _logout(hub, "sk-a")).status_code == 200

    # Другие ключи — свой и чужой — продолжают работать.
    assert (await hub.get("/api/me", headers=bearer("sk-b"))).status_code == 200
    assert (await hub.get("/api/me", headers=bearer("sk-u2"))).status_code == 200
    assert (await hub.get("/api/me", headers=bearer("sk-a"))).status_code == 401

    # Подключение и токен целевой системы не тронуты, отзыв в целевую систему не запускался.
    assert await fetch_rows(hub.app, "SELECT * FROM connections WHERE user_id = 'u1'") == before_conn
    assert await fetch_rows(hub.app, "SELECT * FROM upstream_tokens") == before_upstream
    assert hub.net.verify.calls == verify_calls
    assert len(hub.net.tokens.revoke_requests) == revoke_calls, "запущен отзыв в целевой системе"

    # Клиентский токен Hub продолжает работать, веб-сессия жива.
    proxied = await hub.post(
        "/mcp/tag", content=jsonrpc_body("tools/list"), headers=mcp_headers(tokens["access_token"])
    )
    assert proxied.status_code == 200, proxied.text
    assert "result" in proxied.json()
    assert (await hub.get("/ui/connections")).status_code == 200

    # В LiteLLM ушёл ровно один запрос и только со значением вышедшего ключа.
    assert route.call_count == 1
    assert key_delete_calls(hub.litellm) == [{"keys": ["sk-a"]}]


# --- AC-247: выход доступен только по ключу --------------------------------


@pytest.mark.ac("AC-247")
async def test_logout_requires_a_key_and_refuses_a_web_session(hub: Hub) -> None:
    """Без заголовка, с неизвестным ключом и по одной лишь cookie — 401 (R-L11.2, решение 111)."""
    from tests.support import web_login

    await seed_user_with_key(hub.app, "sk-live", user_id="u1")
    csrf = await web_login(hub, "u1")
    route = mock_key_delete(hub.litellm)

    refusals = {
        "без заголовка": await hub.client.delete(LOGOUT),
        "неизвестный ключ": await hub.client.delete(LOGOUT, headers=bearer("sk-unknown")),
        "только cookie": await hub.client.delete(LOGOUT, headers={"X-CSRF-Token": csrf}),
    }
    for title, response in refusals.items():
        assert response.status_code == 401, f"{title}: {response.text}"
        body = response.json()
        assert body["error"] == "unauthorized", title
        assert body["hint"] == "выполните вход: opencode corp login", title
        assert response.headers.get("www-authenticate") == "Bearer", title

    assert route.call_count == 0, "запрос отзыва ушёл при неудачной аутентификации"
    assert len(await _api_keys(hub)) == 1, "строка ключа удалена при отказе"

    accepted = await _logout(hub, "sk-live")
    assert accepted.status_code == 200, accepted.text
    assert route.call_count == 1


@pytest.mark.ac("AC-247")
async def test_web_logout_still_touches_only_the_web_session(hub: Hub) -> None:
    """``POST /auth/logout`` по-прежнему завершает только веб-сессию (AC-137 не изменился)."""
    from tests.support import web_login

    await seed_user_with_key(hub.app, "sk-live", user_id="u1")
    csrf = await web_login(hub, "u1")
    route = mock_key_delete(hub.litellm)

    response = await hub.client.post("/auth/logout", headers={"X-CSRF-Token": csrf})
    assert response.status_code in (200, 302, 303), response.text
    assert route.call_count == 0, "браузерный выход тронул ключи LiteLLM"
    assert len(await _api_keys(hub)) == 1
    assert (await hub.get("/api/me", headers=bearer("sk-live"))).status_code == 200


# --- AC-248: отзыв прежних ключей при повторном входе ----------------------


@pytest.mark.ac("AC-248")
async def test_relogin_revokes_the_previous_key_by_default(hub: Hub) -> None:
    """Умолчание: прежний постоянный ключ отзывается по алиасу тем же SSO-JWT (R-L12)."""
    route = mock_key_delete(hub.litellm)
    jwt_first = await _login(hub, "sk-1", ll_id="ll-1")
    rows = await _api_keys(hub, "u1")
    assert len(rows) == 1
    previous_alias = rows[0]["key_alias"]

    hub.clock.advance(120)
    jwt_second = await _login(hub, "sk-2", ll_id="ll-2")
    assert jwt_second != jwt_first

    # Ровно один запрос отзыва — по алиасу, а не по значению (значений Hub не хранит).
    assert route.call_count == 1
    assert key_delete_calls(hub.litellm) == [{"key_aliases": [previous_alias]}]
    # Тем же SSO-JWT, которым выполнялся /key/generate (R-L12.3).
    assert key_delete_credentials(hub.litellm) == [f"Bearer {jwt_second}"]

    # R-L12.4: отзыв идёт ПОСЛЕ запроса выпуска.
    paths = litellm_paths(hub.litellm)
    assert paths.index("/key/delete") > paths.index("/key/generate"), paths

    # Прежний ключ немедленно перестал открывать Hub, новый работает (R-L12.5).
    assert (await hub.get("/api/me", headers=bearer("sk-1"))).status_code == 401
    assert (await hub.get("/api/me", headers=bearer("sk-2"))).status_code == 200
    remaining = await _api_keys(hub, "u1")
    assert len(remaining) == 1 and remaining[0]["key_alias"] != previous_alias


@pytest.mark.ac("AC-248")
async def test_relogin_within_the_same_minute_never_revokes_the_new_key_by_alias(hub: Hub) -> None:
    """Два входа в пределах одной минуты: алиас коллизирует — и именно поэтому не отзывается.

    Сторожевой сценарий на баг из ``reports/review-rev43-1.json`` (финдинг 1/2, инъекция 9),
    починенный коммитом ``b93410a``. Алиас строится с точностью до минуты (``key_alias``,
    ``src/hub/login.py``), а исключение «только что созданной» строки обязано быть по алиасу, а не
    только по ``key_sha256`` — иначе LiteLLM отозвал бы по этому алиасу и прежний ключ, и новый.
    Все прежние сценарии повторного входа делают ``hub.clock.advance(120)``, поэтому коллизия по
    построению недостижима и настоящий баг проходил мимо тестов незамеченным.
    """
    route = mock_key_delete(hub.litellm)
    await _login(hub, "sk-1", ll_id="ll-1")
    previous_alias = (await _api_keys(hub, "u1"))[0]["key_alias"]
    await _warm_key_cache(hub, "sk-1")

    hub.clock.advance(5)  # тот же час:минута — алиас второго входа совпадёт с первым
    await _login(hub, "sk-2", ll_id="ll-2")
    new_alias = (await _api_keys(hub, "u1"))[0]["key_alias"]
    # Сторож самой проверки: без реальной коллизии алиасов испытание ничего не проверяет.
    assert new_alias == previous_alias, "тест не достиг коллизии алиасов (проверьте формат/advance)"

    # Алиас нового ключа в key_aliases не попадает никогда; отзывать после исключения нечего —
    # запрос в LiteLLM не отправляется вовсе (не «пустой список алиасов», а НОЛЬ запросов).
    assert route.call_count == 0, "ушёл запрос отзыва, хотя единственный кандидат — сам новый ключ"
    assert key_delete_calls(hub.litellm) == []

    # Новый ключ работает, прежний — нет и немедленно; кэш прежнего ключа сброшен.
    assert (await hub.get("/api/me", headers=bearer("sk-2"))).status_code == 200
    assert (await hub.get("/api/me", headers=bearer("sk-1"))).status_code == 401
    assert await hub.app.state.kv.get(f"keyauth:{sha256_hex('sk-1')}") is None

    # Строка прежнего ключа удалена; в api_keys u1 осталась только новая.
    remaining = await _api_keys(hub, "u1")
    assert len(remaining) == 1
    assert remaining[0]["key_alias"] == new_alias

    # В аудите — ровно skipped-запись с алиасом коллизии, ok/failed по нему нет.
    revoked_rows = await audit_rows(hub.app, "key_revoked")
    assert [r["details"] for r in revoked_rows] == [
        {"key_alias": previous_alias, "reason": "relogin", "outcome": "skipped"}
    ]


@pytest.mark.ac("AC-248")
async def test_relogin_within_the_same_minute_mixed_with_a_distinct_previous_key(hub: Hub) -> None:
    """Смешанный случай: коллизия по алиасу вместе с прежним ключом другого алиаса.

    Ровно один запрос отзыва, и в нём только чужой (не коллизирующий) алиас; в аудите — ``ok`` для
    него и ``skipped`` для алиаса-двойника нового ключа; оба прежних ключа перестают открывать Hub.
    """
    route = mock_key_delete(hub.litellm)
    await _login(hub, "sk-1", ll_id="ll-1")
    previous_alias = (await _api_keys(hub, "u1"))[0]["key_alias"]
    # Прежний ключ другой установки/времени добавлен НАПРЯМУЮ (не через вход), иначе он был бы
    # отозван уже первым входом — коллизия нужна только для алиаса sk-1, не для sk-legacy.
    await insert_key(hub.app, "sk-legacy", "u1", key_alias="opencode-u1-20260101-0000")
    assert len(await _api_keys(hub, "u1")) == 2

    hub.clock.advance(5)  # тот же час:минута — алиас второго входа совпадёт с алиасом sk-1
    await _login(hub, "sk-2", ll_id="ll-2")
    new_alias = (await _api_keys(hub, "u1"))[0]["key_alias"]
    assert new_alias == previous_alias, "тест не достиг коллизии алиасов (проверьте формат/advance)"

    # Один запрос — и только со значением чужого (не коллизирующего) алиаса.
    assert route.call_count == 1
    assert key_delete_calls(hub.litellm) == [{"key_aliases": ["opencode-u1-20260101-0000"]}]

    assert (await hub.get("/api/me", headers=bearer("sk-2"))).status_code == 200
    assert (await hub.get("/api/me", headers=bearer("sk-1"))).status_code == 401
    assert (await hub.get("/api/me", headers=bearer("sk-legacy"))).status_code == 401

    remaining = await _api_keys(hub, "u1")
    assert len(remaining) == 1 and remaining[0]["key_alias"] == new_alias

    revoked_rows = await audit_rows(hub.app, "key_revoked")
    by_alias = {r["details"]["key_alias"]: r["details"]["outcome"] for r in revoked_rows}
    assert by_alias == {
        "opencode-u1-20260101-0000": "ok",
        previous_alias: "skipped",
    }
    assert len(revoked_rows) == 2, "лишние или недостающие записи в аудите"


@pytest.mark.ac("AC-248")
async def test_relogin_without_the_setting_keeps_the_previous_contract(
    make_hub: HubFactory,
) -> None:
    """``false`` возвращает прежний контракт I-1: ключ добавляется, запросов отзыва нет (R-L12.1)."""
    hub = await make_hub(login_revokes_previous_keys=False)
    route = mock_key_delete(hub.litellm)
    await _login(hub, "sk-1", ll_id="ll-1")
    hub.clock.advance(120)
    await _login(hub, "sk-2", ll_id="ll-2")

    for key in ("sk-1", "sk-2"):
        assert (await hub.get("/api/me", headers=bearer(key))).status_code == 200, key
    assert len(await _api_keys(hub, "u1")) == 2
    assert route.call_count == 0
    assert await audit_rows(hub.app, "key_revoked") == []


@pytest.mark.ac("AC-248")
async def test_failed_revocation_does_not_break_the_login(hub: Hub) -> None:
    """Неудача отзыва вход не ломает; след остаётся в аудите (R-L12.4, R-L12.6)."""
    mock_key_delete(hub.litellm, status=500, body={"detail": "boom"})
    await _login(hub, "sk-1", ll_id="ll-1")
    previous_alias = (await _api_keys(hub, "u1"))[0]["key_alias"]
    hub.clock.advance(120)

    mock_start(hub.litellm, start_body(login_id="ll-2"))
    start = await hub.post("/cli/start", json={"client": "opencode-fork/1.17.9"})
    jwt = make_jwt({"sub": "u1", "email": "u1@corp.test", "exp": int(hub.clock.time()) + 3600})
    mock_poll(hub.litellm, ready_body(jwt, user_id="u1", team_id="t1"), login_id="ll-2")
    mock_key_generate(hub.litellm, "sk-2")
    polled = await hub.poll(start.json()["login_id"], start.json()["poll_secret"])

    # Вход успешен и поля ответа ready не изменились от неудачи отзыва.
    assert polled.status_code == 200, polled.text
    body = polled.json()
    assert body["status"] == "ready"
    assert body["key"] == "sk-2"
    assert body["key_kind"] == "persistent"
    assert body["user"]["user_id"] == "u1"

    # Строка прежнего ключа всё равно удалена — он обязан перестать открывать Hub (R-L12.5).
    assert (await hub.get("/api/me", headers=bearer("sk-1"))).status_code == 401
    assert len(await _api_keys(hub, "u1")) == 1
    revoked = await audit_rows(hub.app, "key_revoked")
    assert [r["details"] for r in revoked] == [
        {"key_alias": previous_alias, "reason": "relogin", "outcome": "failed"}
    ]


@pytest.mark.ac("AC-248")
async def test_jwt_keys_are_never_revoked(hub: Hub) -> None:
    """Ключи ``key_kind: jwt`` Hub не выпускал и не отзывает никогда (R-L12.2, решение 114)."""
    await seed_user_with_key(hub.app, "sk-jwt-key", user_id="u1", key_kind="jwt",
                             key_alias="opencode-u1-20260101-1000")
    route = mock_key_delete(hub.litellm)
    await _login(hub, "sk-new", ll_id="ll-1")

    assert route.call_count == 0, "в отзыв попал ключ, который Hub не выпускал"
    # Строка jwt-ключа не тронута, и он продолжает аутентифицировать.
    kinds = {r["key_kind"] for r in await _api_keys(hub, "u1")}
    assert kinds == {"jwt", "persistent"}
    assert (await hub.get("/api/me", headers=bearer("sk-jwt-key"))).status_code == 200


@pytest.mark.ac("AC-248")
async def test_key_with_foreign_alias_prefix_is_not_revoked(hub: Hub) -> None:
    """Отзывается только своё: ключ с чужим префиксом алиаса не трогается (решение 114)."""
    await add_key(hub, "sk-anchor", "u1")
    await insert_key(hub.app, "sk-foreign", "u1", key_alias="другой-hub-u1-20260101-1000")
    route = mock_key_delete(hub.litellm)
    await _login(hub, "sk-new", ll_id="ll-1")

    aliases = {c for call in key_delete_calls(hub.litellm) for c in call.get("key_aliases", [])}
    assert "другой-hub-u1-20260101-1000" not in aliases, "отозван ключ чужой установки"
    assert route.call_count == 1


@pytest.mark.ac("AC-248")
async def test_login_after_logout_sends_no_revocation_at_all(hub: Hub) -> None:
    """R-L12.7: после выхода отзывать нечего — запрос не отправляется вовсе."""
    route = mock_key_delete(hub.litellm)
    await _login(hub, "sk-1", ll_id="ll-1")
    assert (await _logout(hub, "sk-1")).status_code == 200
    assert route.call_count == 1  # запрос выхода
    assert await _api_keys(hub, "u1") == []

    hub.clock.advance(120)
    await _login(hub, "sk-2", ll_id="ll-2")
    assert route.call_count == 1, "вход после выхода отправил лишний запрос отзыва"
    assert (await hub.get("/api/me", headers=bearer("sk-2"))).status_code == 200


@pytest.mark.ac("AC-248")
async def test_other_users_keys_are_never_revoked(hub: Hub) -> None:
    """Отзыв ограничен ключами того же пользователя (R-L12.2)."""
    await seed_user_with_key(hub.app, "sk-u2", user_id="u2", email="u2@corp.test")
    route = mock_key_delete(hub.litellm)
    await _login(hub, "sk-1", ll_id="ll-1")
    hub.clock.advance(120)
    await _login(hub, "sk-2", ll_id="ll-2")

    foreign_alias = (await _api_keys(hub, "u2"))[0]["key_alias"]
    aliases = {c for call in key_delete_calls(hub.litellm) for c in call.get("key_aliases", [])}
    assert foreign_alias not in aliases, "в отзыв попал алиас чужого пользователя"
    assert (await hub.get("/api/me", headers=bearer("sk-u2"))).status_code == 200, "чужой ключ отозван"
    assert len(await _api_keys(hub, "u2")) == 1
    assert route.call_count == 1


# --- AC-249: секреты не попадают в ответы, журнал и аудит ------------------


ADMIN_KEY = "sk-admin-SECRET"
USER_KEY = "sk-out-SECRET"
BODY_MARKER = "BODY-MARKER-L"


@pytest.mark.ac("AC-249")
async def test_key_values_hashes_and_admin_key_never_leak(
    make_hub: HubFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Значения ключей, их sha256 и служебный ключ LiteLLM нигде не появляются (R-L11.9, R-K3).

    Значения ключей и тело ответа LiteLLM ищутся по записям **всех** логгеров: утечка через
    стороннюю библиотеку — тоже утечка. Хеш ключа ищется по журналу Hub: он лежит открытым в
    ``api_keys.key_sha256`` и потому законно виден в DEBUG-эхе SQL драйвера БД — тот же разбор,
    что принят ревью для ``issued_token_id`` в AC-224.
    """
    hub = await make_hub(litellm_admin_key=ADMIN_KEY)
    await seed_user_with_key(hub.app, USER_KEY, user_id="u1")
    await _warm_key_cache(hub, USER_KEY)
    mock_key_delete(
        hub.litellm, body={"message": f"{USER_KEY} deleted", "extra": BODY_MARKER}
    )
    capture_all_levels(caplog)

    with capture_json_logs() as json_logs:
        logged_out = await _logout(hub, USER_KEY)
        assert logged_out.status_code == 200, logged_out.text
        # Повторный вход с отзывом прежнего ключа — второй путь, где ключи попадают в обработку.
        await _login(hub, "sk-1", ll_id="ll-1")
        hub.clock.advance(120)
        await _login(hub, "sk-2", ll_id="ll-2")
        responses = [
            logged_out,
            await hub.get("/api/me", headers=bearer("sk-2")),
            await hub.get("/metrics"),
            await hub.get("/health"),
        ]

    digest = sha256_hex(USER_KEY)
    secrets = (USER_KEY, ADMIN_KEY, BODY_MARKER, digest)
    for response in responses:
        assert response.status_code == 200, response.text
        for secret in secrets:
            assert secret not in response.text, f"{response.url}: {secret}"

    everything = "\n".join([record_text(r) for r in caplog.records] + json_logs.raw())
    assert everything, "журнал пуст — проверка вырождена"
    for secret in (USER_KEY, ADMIN_KEY, BODY_MARKER):
        assert secret not in everything, secret
    assert digest not in hub_log(caplog, json_logs), "хеш ключа попал в журнал Hub"

    rows = await audit_rows(hub.app)
    dumped = json.dumps(rows, default=str, ensure_ascii=False)
    for secret in secrets:
        assert secret not in dumped, f"{secret} в audit_log"
    allowed_logout = {"key_kind", "key_alias", "revoked", "revoke_error"}
    allowed_revoked = {"key_alias", "reason", "outcome"}
    for row in rows:
        if row["action"] == "logout":
            assert set(row["details"]) == allowed_logout, row
        if row["action"] == "key_revoked":
            assert set(row["details"]) == allowed_revoked, row

    # R-L11.9: в журнале есть запись с user_id, key_alias и кодом ответа LiteLLM.
    records = [r for r in json_logs.records() if r.get("message") == "key_revoke"]
    assert records, json.dumps(json_logs.records(), ensure_ascii=False)[:400]
    assert records[0]["user_id"] == "u1"
    assert records[0]["key_alias"] == "opencode-u1-20260101-1200"
    assert records[0]["status"] == 200

    # Служебный ключ задан — отзыв идёт им, а не значением пользовательского ключа (R-L11.4а).
    assert key_delete_credentials(hub.litellm)[0] == f"Bearer {ADMIN_KEY}"


@pytest.mark.ac("AC-249")
async def test_admin_key_is_used_for_relogin_revocation_too(make_hub: HubFactory) -> None:
    """Служебный ключ, если задан, используется и при отзыве прежних ключей входа (R-L12.3)."""
    hub = await make_hub(litellm_admin_key=ADMIN_KEY)
    mock_key_delete(hub.litellm)
    await _login(hub, "sk-1", ll_id="ll-1")
    hub.clock.advance(120)
    await _login(hub, "sk-2", ll_id="ll-2")
    assert key_delete_credentials(hub.litellm) == [f"Bearer {ADMIN_KEY}"]


# --- AC-250: больше двадцати прежних ключей --------------------------------


# Наследство установки, где настройка долго была выключена: 25 прежних постоянных ключей.
LEGACY_KEYS = 25
# Порядок ``created_at`` намеренно расходится с порядком вставки (и с ``id``): смещение
# ``(7 * i) % 25`` — биекция, поэтому ни «первые по id», ни «последние по id» не совпадают
# с «самыми свежими по created_at». Без такого расхождения отбор по id прошёл бы проверку.
CREATED_OFFSETS = [(7 * index) % LEGACY_KEYS for index in range(LEGACY_KEYS)]


async def _seed_legacy_keys(hub: Hub, count: int = LEGACY_KEYS) -> list[tuple[str, str, Any]]:
    """``count`` прежних постоянных ключей u1; вернуть ``(ключ, алиас, created_at)``."""
    from datetime import timedelta

    from tests.support import insert_user

    await insert_user(hub.app, "u1", "u1@corp.test")
    base = hub.clock.now() - timedelta(days=30)
    seeded: list[tuple[str, str, Any]] = []
    for index in range(count):
        key = f"sk-old-{index:02d}"
        alias = f"opencode-u1-legacy-{index:02d}"
        created = base + timedelta(minutes=CREATED_OFFSETS[index] if count == LEGACY_KEYS else index)
        await insert_key(hub.app, key, "u1", key_alias=alias, created_at=created)
        seeded.append((key, alias, created))
    return seeded


@pytest.mark.ac("AC-250")
async def test_more_than_twenty_previous_keys_revoke_freshest_and_audit_the_rest(
    hub: Hub, caplog: pytest.LogCaptureFixture
) -> None:
    """Отзываются 20 самых свежих по ``created_at``, остальные — в аудит как пропущенные.

    Резолюция диспута ``reports/dispute-rev43-R-L12-alias-limit.md`` (решение 118): остатка «на
    следующий вход» не существует — строки удаляются все, поэтому по каждому неотозванному алиасу
    остаётся запись аудита, единственный след для ручной уборки администратором LiteLLM.
    """
    from hub.litellm import REVOKE_ALIAS_LIMIT

    seeded = await _seed_legacy_keys(hub)
    assert len(seeded) == LEGACY_KEYS
    # Ожидание строится сортировкой по created_at, а не порядком вставки (R-L12.3).
    by_freshness = [alias for _key, alias, _created in sorted(seeded, key=lambda e: e[2], reverse=True)]
    expected_revoked = by_freshness[:REVOKE_ALIAS_LIMIT]
    expected_skipped = by_freshness[REVOKE_ALIAS_LIMIT:]
    assert len(expected_skipped) == LEGACY_KEYS - REVOKE_ALIAS_LIMIT == 5
    # Сторож самой проверки: отбор по id дал бы другой набор — иначе инъекция была бы незаметна.
    by_insertion = [alias for _key, alias, _created in seeded]
    assert set(expected_revoked) != set(by_insertion[-REVOKE_ALIAS_LIMIT:])
    assert set(expected_revoked) != set(by_insertion[:REVOKE_ALIAS_LIMIT])

    route = mock_key_delete(hub.litellm)
    capture_all_levels(caplog)
    with capture_json_logs() as json_logs:
        await _login(hub, "sk-new", ll_id="ll-1")

    # Один запрос, ровно 20 алиасов — двадцать самых свежих, без алиаса нового ключа.
    assert route.call_count == 1
    calls = key_delete_calls(hub.litellm)
    assert len(calls) == 1
    sent = calls[0]["key_aliases"]
    assert len(sent) == REVOKE_ALIAS_LIMIT
    assert set(sent) == set(expected_revoked), "в запрос попали не самые свежие по created_at"
    assert sent == expected_revoked, "порядок отбора не по убыванию created_at"
    new_alias = (await _api_keys(hub, "u1"))[0]["key_alias"]
    assert new_alias not in sent

    # Локально не осталось ни одной прежней строки; все 25 прежних ключей мертвы (R-L12.5).
    assert len(await _api_keys(hub, "u1")) == 1
    assert (await hub.get("/api/me", headers=bearer("sk-new"))).status_code == 200
    for key, _alias, _created in seeded:
        assert (await hub.get("/api/me", headers=bearer(key))).status_code == 401, key

    # Аудит: 20 ok и 5 skipped — по одной записи на каждый неотозванный алиас (R-L12.4).
    revoked_rows = await audit_rows(hub.app, "key_revoked")
    ok_rows = [r for r in revoked_rows if r["details"]["outcome"] == "ok"]
    skipped_rows = [r for r in revoked_rows if r["details"]["outcome"] == "skipped"]
    assert len(ok_rows) == REVOKE_ALIAS_LIMIT
    assert len(skipped_rows) == 5
    assert len(revoked_rows) == LEGACY_KEYS, "появились записи сверх двадцати пяти алиасов"
    assert {r["details"]["key_alias"] for r in ok_rows} == set(expected_revoked)
    assert {r["details"]["key_alias"] for r in skipped_rows} == set(expected_skipped)
    for row in revoked_rows:
        assert row["details"]["reason"] == "relogin"
        assert set(row["details"]) == {"key_alias", "reason", "outcome"}

    # Журнал: одна запись WARNING с числом пропущенных — без алиасов, значений и хешей.
    skipped_records = [
        r for r in json_logs.records() if r.get("message") == "previous_keys_revoke_skipped"
    ]
    assert len(skipped_records) == 1, json_logs.raw()
    record = skipped_records[0]
    assert record["level"] == "WARNING"
    assert record["skipped"] == 5
    assert record["user_id"] == "u1"
    dumped = json.dumps(record, ensure_ascii=False)
    for alias in by_freshness:
        assert alias not in dumped, f"алиас {alias} попал в запись о пропущенных"

    everything = "\n".join([record_text(r) for r in caplog.records] + json_logs.raw())
    for key, _alias, _created in seeded:
        assert key not in everything, f"значение ключа {key} в журнале"
    hub_only = hub_log(caplog, json_logs)
    for key, _alias, _created in seeded:
        assert sha256_hex(key) not in hub_only, "хеш ключа в журнале Hub"

    # Остатка не существует: второй вход отзывает ровно один алиас — ключа первого входа.
    hub.clock.advance(120)
    await _login(hub, "sk-newer", ll_id="ll-2")
    second = key_delete_calls(hub.litellm)[1]["key_aliases"]
    assert second == [new_alias], (
        f"во второй запрос попал не только ключ предыдущего входа: {second}"
    )


@pytest.mark.ac("AC-250")
async def test_without_overflow_there_are_no_skipped_records(
    hub: Hub, caplog: pytest.LogCaptureFixture
) -> None:
    """Лимит не переполнен — записей ``skipped`` нет вовсе, и WARNING о пропуске тоже."""
    from hub.litellm import REVOKE_ALIAS_LIMIT

    seeded = await _seed_legacy_keys(hub, count=REVOKE_ALIAS_LIMIT)
    mock_key_delete(hub.litellm)
    capture_all_levels(caplog)
    with capture_json_logs() as json_logs:
        await _login(hub, "sk-new", ll_id="ll-1")

    revoked_rows = await audit_rows(hub.app, "key_revoked")
    assert len(revoked_rows) == len(seeded)
    assert {r["details"]["outcome"] for r in revoked_rows} == {"ok"}
    assert [r for r in revoked_rows if r["details"]["outcome"] == "skipped"] == []
    assert [
        r for r in json_logs.records() if r.get("message") == "previous_keys_revoke_skipped"
    ] == [], "запись о пропущенных при непереполненном лимите"
