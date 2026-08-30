"""Требование заказчика (сформулировано 30.08): у пользователя ровно один ключ приложения.

Повторный вход не должен плодить второй ключ приложения — прежний отзывается, а всё, что ключом
приложения не является (чужой ключ, ключ с чужим алиасом, ключ другого пользователя, невыпущенный
Hub'ом ``jwt``-ключ), обязано остаться нетронутым. Механизм — ``LoginService._revoke_previous_keys``
(``src/hub/login.py``, R-L12). Пять сторожей ниже проверяют это наблюдением: что осталось в
``api_keys``, чем ключ отвечает на ``GET /api/me`` и что ушло в тело ``POST /key/delete`` LiteLLM —
без обращения к внутренностям реализации.

Привязка к критериям приёмки: AC-248 (R-L12.1–R-L12.2, R-L12.7 — что отзывается и что нет) и
AC-251 (R-L12.2 — алиас свежего ключа никогда не отзывается сам).
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import Hub
from tests.support import (
    bearer,
    fetch_rows,
    insert_key,
    key_delete_calls,
    make_jwt,
    mock_key_delete,
    mock_key_generate,
    mock_poll,
    mock_start,
    ready_body,
    seed_user_with_key,
    start_body,
)


async def _api_keys(hub: Hub, user_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT user_id, key_sha256, key_alias, key_kind FROM api_keys"
    if user_id is not None:
        return await fetch_rows(hub.app, sql + " WHERE user_id = :u ORDER BY id", u=user_id)
    return await fetch_rows(hub.app, sql + " ORDER BY id")


async def _login(hub: Hub, key: str, *, ll_id: str, user_id: str = "u1") -> None:
    """Полный вход через CLI-SSO для ``user_id``, завершающийся выдачей ``key``."""
    mock_start(hub.litellm, start_body(login_id=ll_id))
    start = await hub.post("/cli/start", json={"client": "opencode-fork/1.17.9"})
    assert start.status_code == 200, start.text
    body = start.json()
    jwt = make_jwt({"sub": user_id, "email": f"{user_id}@corp.test", "exp": int(hub.clock.time()) + 3600})
    mock_poll(hub.litellm, ready_body(jwt, user_id=user_id, team_id="t1"), login_id=ll_id)
    mock_key_generate(hub.litellm, key)
    polled = await hub.poll(body["login_id"], body["poll_secret"])
    assert polled.status_code == 200, polled.text
    assert polled.json()["key"] == key


def _all_revoked_aliases(hub: Hub) -> set[str]:
    return {a for call in key_delete_calls(hub.litellm) for a in call.get("key_aliases", [])}


# --- G1: повторный вход оставляет ровно один ключ приложения ---------------


@pytest.mark.ac("AC-248")
async def test_relogin_leaves_exactly_one_fresh_app_key(hub: Hub) -> None:
    """У пользователя после повторного входа ровно один постоянный ключ приложения — свежий.

    Прежний ключ приложения удалён из ``api_keys``, немедленно перестаёт открывать Hub, а его
    алиас ушёл в тело ``POST /key/delete`` LiteLLM (R-L12.1, R-L12.2, R-L12.5).
    """
    mock_key_delete(hub.litellm)
    await _login(hub, "sk-1", ll_id="ll-1")
    previous_alias = (await _api_keys(hub, "u1"))[0]["key_alias"]

    hub.clock.advance(120)  # развести алиасы по минуте — не коллизия AC-251
    await _login(hub, "sk-2", ll_id="ll-2")

    remaining = await _api_keys(hub, "u1")
    assert len(remaining) == 1, f"ожидался ровно один ключ приложения, найдено {len(remaining)}"
    assert remaining[0]["key_alias"] != previous_alias

    assert (await hub.get("/api/me", headers=bearer("sk-1"))).status_code == 401
    assert (await hub.get("/api/me", headers=bearer("sk-2"))).status_code == 200

    assert previous_alias in _all_revoked_aliases(hub), "алиас прежнего ключа не ушёл в отзыв"


# --- G2 (главный сторож): чужой по алиасу ключ того же пользователя не трогается ----


@pytest.mark.ac("AC-248")
async def test_key_with_non_app_alias_prefix_survives_relogin(hub: Hub) -> None:
    """Ключ того же пользователя с алиасом НЕ из префикса приложения переживает повторный вход.

    Именно этот сторож — предмет требования заказчика: ключ, который приложение не выпускало
    (алиас другой установки/инструмента), обязан остаться в ``api_keys`` и продолжать открывать
    Hub, а его алиас не должен попасть в тело запроса отзыва LiteLLM (R-L12.2, решение 114).
    """
    await seed_user_with_key(hub.app, "sk-app-1", user_id="u1")  # свой ключ приложения — есть что отзывать
    await insert_key(hub.app, "sk-foreign-tool", "u1", key_alias="othertool-u1-20260101-0900")
    assert (await hub.get("/api/me", headers=bearer("sk-foreign-tool"))).status_code == 200

    mock_key_delete(hub.litellm)
    await _login(hub, "sk-app-2", ll_id="ll-1")

    # Строка чужого по алиасу ключа осталась ровно одна и не изменилась.
    foreign_rows = await fetch_rows(
        hub.app,
        "SELECT user_id, key_alias, key_kind FROM api_keys WHERE key_alias = :a",
        a="othertool-u1-20260101-0900",
    )
    assert len(foreign_rows) == 1, "строка ключа с чужим префиксом алиаса пропала или задвоилась"

    # Ключ по-прежнему открывает Hub.
    assert (await hub.get("/api/me", headers=bearer("sk-foreign-tool"))).status_code == 200

    # Его алиас ни разу не встретился в теле запроса отзыва.
    assert "othertool-u1-20260101-0900" not in _all_revoked_aliases(hub)


# --- G3: ключ ДРУГОГО пользователя не трогается -----------------------------


@pytest.mark.ac("AC-248")
async def test_other_users_key_survives_relogin_and_is_never_sent_to_revoke(hub: Hub) -> None:
    """Ключ приложения другого пользователя переживает чужой повторный вход (R-L12.2)."""
    await seed_user_with_key(
        hub.app, "sk-u2", user_id="u2", email="u2@corp.test", key_alias="opencode-u2-20260101-0800"
    )
    foreign_alias = (await _api_keys(hub, "u2"))[0]["key_alias"]

    mock_key_delete(hub.litellm)
    await _login(hub, "sk-1", ll_id="ll-1", user_id="u1")
    hub.clock.advance(120)
    await _login(hub, "sk-2", ll_id="ll-2", user_id="u1")

    assert (await hub.get("/api/me", headers=bearer("sk-u2"))).status_code == 200
    assert len(await _api_keys(hub, "u2")) == 1
    assert foreign_alias not in _all_revoked_aliases(hub), "алиас чужого пользователя ушёл в отзыв"


# --- G4: ключ того же пользователя с key_kind != persistent не трогается ---


@pytest.mark.ac("AC-248")
async def test_non_persistent_key_kind_survives_relogin(hub: Hub) -> None:
    """``key_kind = 'jwt'`` Hub не выпускал и под отзыв не попадает никогда (R-L12.2, решение 114)."""
    await seed_user_with_key(
        hub.app, "sk-jwt-fallback", user_id="u1", key_kind="jwt",
        key_alias="opencode-u1-20260101-0900",
    )

    mock_key_delete(hub.litellm)
    await _login(hub, "sk-app-1", ll_id="ll-1")

    kinds = {r["key_kind"] for r in await _api_keys(hub, "u1")}
    assert "jwt" in kinds, "строка jwt-ключа пропала"
    assert (await hub.get("/api/me", headers=bearer("sk-jwt-fallback"))).status_code == 200
    assert "opencode-u1-20260101-0900" not in _all_revoked_aliases(hub)


# --- G5: свежевыданный ключ не отзывается сам (AC-251, решение 119) --------


@pytest.mark.ac("AC-251")
async def test_freshly_issued_key_is_never_revoked_by_its_own_alias(hub: Hub) -> None:
    """Повторный вход в пределах одной минуты: алиас нового ключа совпадает со старым.

    Свежий ключ не должен отозвать сам себя — единственный кандидат на отзыв (алиас-двойник)
    исключён, и раз отзывать больше нечего, запрос в LiteLLM не отправляется вовсе (R-L12.2,
    R-L12.3, решение 119 по диспуту ``disputes/spec-dispute-R-L12-alias-collision.json``).
    """
    route = mock_key_delete(hub.litellm)
    await _login(hub, "sk-prev", ll_id="ll-1")
    previous_alias = (await _api_keys(hub, "u1"))[0]["key_alias"]

    hub.clock.advance(5)  # тот же час:минута — алиас второго входа совпадёт с первым
    await _login(hub, "sk-fresh", ll_id="ll-2")
    new_alias = (await _api_keys(hub, "u1"))[0]["key_alias"]
    assert new_alias == previous_alias, "тест не достиг коллизии алиасов (проверьте advance)"

    assert route.call_count == 0, "ушёл запрос отзыва по алиасу самого свежего ключа"
    assert new_alias not in _all_revoked_aliases(hub)
    assert (await hub.get("/api/me", headers=bearer("sk-fresh"))).status_code == 200
