"""Страницы Hub различают ожидание, потерю связи и сорванный вход (R-W2, R-W6): BUG-I3-003.

Страница входа неограниченно долго показывала «Ожидаем подтверждения входа…», хотя Hub был
недоступен, а её сессия входа уже не существовала: htmx при сбое запроса ничего не подменяет, а
обработчиков ошибок не было. Здесь закрепляется серверная часть исправления — у каждого сорванного
входа есть действие «Начать вход заново» с сохранением ``next`` — и разметка с обработчиками,
на которую опирается клиентская часть.

Клиентское поведение (плашка «Связь с Hub потеряна», гашение устаревшего блока, автоповтор
выборки) исполняется браузером и серверным тестом не проверяется: см. отчёт, раздел о границах.

Все проверки идут против локальных моков; обращений в сеть нет.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from hub.login import SESSION_PREFIX
from hub.routes.web import WEB_LOGIN_PREFIX
from tests.conftest import Hub, HubFactory
from tests.support import (
    CATALOG_ENV,
    WEB_LOGIN_POLL_RE,
    i3_catalog,
    make_jwt,
    mock_key_generate,
    mock_poll,
    mock_start,
    ready_body,
    start_body,
    teams_body,
)

BASE_TEMPLATE = Path(__file__).resolve().parents[1] / "src" / "hub" / "templates" / "base.html"

RESTART_TEXT = "Начать вход заново"
WAITING_TEXT = "Ожидаем подтверждения входа"
EXPIRED_TEXT = "Сессия входа истекла"
DEFAULT_NEXT = "/ui/connections"
CUSTOM_NEXT = "/ui/servers/gitlab"

RESTART_HREF_RE = re.compile(r'<a[^>]*class="btn"[^>]*href="([^"]+)"')


async def _hub(make_hub: HubFactory, **overrides: Any) -> Hub:
    return await make_hub(
        catalog=i3_catalog(),
        env=CATALOG_ENV,
        base_url="https://hub.test",
        web_auth="litellm",
        **overrides,
    )


async def _start_login(hub: Hub, *, next_url: str = CUSTOM_NEXT) -> str:
    """Начать вход через CLI-SSO и вернуть ``login_id`` страницы входа."""
    mock_start(hub.litellm, start_body())
    page = await hub.get("/auth/login", params={"next": next_url})
    assert page.status_code == 200, page.text
    match = WEB_LOGIN_POLL_RE.search(page.text)
    assert match, page.text
    return match.group(1)


def _restart_href(html: str) -> str:
    """Адрес действия «Начать вход заново» из фрагмента входа."""
    assert RESTART_TEXT in html, f"во фрагменте нет действия «{RESTART_TEXT}»: {html}"
    match = RESTART_HREF_RE.search(html)
    assert match, f"у действия нет адреса: {html}"
    return match.group(1)


def _assert_restart_keeps_next(html: str, next_url: str) -> None:
    """У сорванного входа есть выход: ссылка на /auth/login с сохранённым ``next``."""
    href = _restart_href(html)
    assert href.startswith("/auth/login?next="), href
    # ``next`` уходит закодированным значением параметра, а не как есть.
    assert href == f"/auth/login?next={next_url.replace('/', '%2F')}", href
    assert WAITING_TEXT not in html, "сорванный вход всё ещё выглядит ожиданием"


# --- ветви ошибок _login_fragment ------------------------------------------


async def test_expired_login_session_offers_a_restart(make_hub: HubFactory) -> None:
    """Записи входа нет вовсе: фрагмент говорит, что сессия истекла, и предлагает начать заново."""
    hub = await _hub(make_hub)
    fragment = await hub.get("/auth/login/poll/no-such-login")
    assert fragment.status_code == 200, fragment.text
    assert EXPIRED_TEXT in fragment.text
    # ``next`` восстановить неоткуда — записи нет; ссылка ведёт на страницу по умолчанию.
    _assert_restart_keeps_next(fragment.text, DEFAULT_NEXT)


async def test_expired_login_session_offers_a_restart_on_team_choice(
    make_hub: HubFactory,
) -> None:
    """То же на выборе команды: POST без записи входа не оставляет пользователя без действия."""
    hub = await _hub(make_hub)
    fragment = await hub.client.post(
        "/auth/login/team/no-such-login", data={"team_id": "t1"}
    )
    assert fragment.status_code == 200, fragment.text
    assert EXPIRED_TEXT in fragment.text
    _assert_restart_keeps_next(fragment.text, DEFAULT_NEXT)


async def test_lost_login_session_keeps_next_from_the_record(make_hub: HubFactory) -> None:
    """Сессия входа пропала (перезапуск Redis), а запись страницы жива: ``next`` сохраняется."""
    hub = await _hub(make_hub)
    login_id = await _start_login(hub)
    # Сессии входа живут в Redis без сохранения на диск — перезапуск обесценивает страницу.
    await hub.app.state.kv.delete(SESSION_PREFIX + login_id)

    fragment = await hub.get(f"/auth/login/poll/{login_id}")
    assert fragment.status_code == 200, fragment.text
    _assert_restart_keeps_next(fragment.text, CUSTOM_NEXT)


async def test_not_ready_status_keeps_next_from_the_record(make_hub: HubFactory) -> None:
    """Целевая система ответила отказом: показан её текст и действие с сохранённым ``next``."""
    hub = await _hub(make_hub)
    login_id = await _start_login(hub)
    mock_poll(hub.litellm, {"status": "denied", "message": "Вход отклонён администратором"})

    fragment = await hub.get(f"/auth/login/poll/{login_id}")
    assert fragment.status_code == 200, fragment.text
    # Текст берётся из службы входа (она нормализует неизвестный статус) — важно, что он есть
    # и что это ветвь ошибки, а не ожидания.
    assert 'class="status login-error"' in fragment.text, fragment.text
    _assert_restart_keeps_next(fragment.text, CUSTOM_NEXT)


async def test_team_choice_failure_keeps_next_from_the_record(make_hub: HubFactory) -> None:
    """Выбор команды не удался: действие «Начать вход заново» есть и здесь."""
    hub = await _hub(make_hub)
    login_id = await _start_login(hub)
    mock_poll(hub.litellm, teams_body(("t1", "Команда 1"), ("t2", "Команда 2")))
    listed = await hub.get(f"/auth/login/poll/{login_id}")
    assert "Команда 1" in listed.text, listed.text

    # Команда вне предложенного списка — служба входа отвечает отказом (invalid_team).
    fragment = await hub.client.post(
        f"/auth/login/team/{login_id}", data={"team_id": "t-нет-такой"}
    )
    assert fragment.status_code == 200, fragment.text
    assert 'class="status login-error"' in fragment.text, fragment.text
    _assert_restart_keeps_next(fragment.text, CUSTOM_NEXT)


async def test_ready_without_user_id_keeps_next_from_the_record(
    make_hub: HubFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ответ «готово» без владельца ключа: четвёртая ветвь ошибки тоже даёт выход.

    Через мок LiteLLM эта ветвь недостижима — служба входа сама отвергает ответ без владельца
    (``login.py``), — поэтому подменяется ровно один шов: результат опроса. Логика маршрута,
    которая и проверяется, остаётся настоящей.
    """
    from hub.login import PollResult

    hub = await _hub(make_hub)
    login_id = await _start_login(hub)

    async def _ready_without_user(*args: Any, **kwargs: Any) -> PollResult:
        return PollResult(200, {"status": "ready", "key": "sk-x", "user": {}})

    monkeypatch.setattr(hub.app.state.login, "poll", _ready_without_user)

    fragment = await hub.get(f"/auth/login/poll/{login_id}")
    assert fragment.status_code == 200, fragment.text
    _assert_restart_keeps_next(fragment.text, CUSTOM_NEXT)


async def test_external_next_is_not_reflected_into_the_restart_link(
    make_hub: HubFactory,
) -> None:
    """Внешний ``next`` не переезжает в ссылку восстановления (R-W1, safe_next)."""
    hub = await _hub(make_hub)
    login_id = await _start_login(hub, next_url="https://evil.test/steal")
    await hub.app.state.kv.delete(SESSION_PREFIX + login_id)

    fragment = await hub.get(f"/auth/login/poll/{login_id}")
    assert fragment.status_code == 200, fragment.text
    href = _restart_href(fragment.text)
    assert "evil.test" not in href, href
    _assert_restart_keeps_next(fragment.text, DEFAULT_NEXT)


# --- ожидание и успех действия не предлагают -------------------------------


async def test_waiting_state_has_no_restart_action(make_hub: HubFactory) -> None:
    """Пока вход идёт, действия «начать заново» нет: оно появляется только у сорванного входа."""
    hub = await _hub(make_hub)
    login_id = await _start_login(hub)
    mock_poll(hub.litellm, {"status": "pending"})

    waiting = await hub.get(f"/auth/login/poll/{login_id}")
    assert waiting.status_code == 200, waiting.text
    assert WAITING_TEXT in waiting.text
    assert RESTART_TEXT not in waiting.text


async def test_team_choice_state_has_no_restart_action(make_hub: HubFactory) -> None:
    """Выбор команды — тоже не сорванный вход: предложения начать заново на нём нет."""
    hub = await _hub(make_hub)
    login_id = await _start_login(hub)
    # Команд должно быть больше одной: единственную служба входа выбирает сама.
    mock_poll(hub.litellm, teams_body(("t1", "Команда 1"), ("t2", "Команда 2")))

    teams = await hub.get(f"/auth/login/poll/{login_id}")
    assert teams.status_code == 200, teams.text
    assert "Команда 1" in teams.text
    assert RESTART_TEXT not in teams.text


async def test_successful_login_still_completes(make_hub: HubFactory) -> None:
    """Штатный вход не задет: ready по-прежнему заводит веб-сессию и уводит на next (AC-133)."""
    hub = await _hub(make_hub)
    login_id = await _start_login(hub)
    claims = {"sub": "u1", "email": "u1@corp.test", "exp": int(hub.clock.time()) + 3600}
    mock_poll(hub.litellm, ready_body(make_jwt(claims), user_id="u1"))
    mock_key_generate(hub.litellm, "sk-web-1")

    done = await hub.get(f"/auth/login/poll/{login_id}")
    assert done.status_code in (200, 302, 303), done.text
    assert RESTART_TEXT not in done.text
    assert await hub.app.state.kv.get(WEB_LOGIN_PREFIX + login_id) is None


# --- разметка и обработчики base.html --------------------------------------


def test_base_template_has_connection_notice_markup() -> None:
    """Плашка состояния связи есть на каждой странице и по умолчанию скрыта (BUG-I3-003)."""
    html = BASE_TEMPLATE.read_text(encoding="utf-8")
    notice = html.split('id="hub-connection"', 1)
    assert len(notice) == 2, "в base.html нет блока #hub-connection"
    tag = notice[0].rsplit("<div", 1)[1] + 'id="hub-connection"' + notice[1].split(">", 1)[0]
    assert "hidden" in tag, "плашка обязана быть скрытой до первого сбоя"
    assert 'role="status"' in tag and 'aria-live="polite"' in tag
    for marker in ('id="hub-connection-title"', 'id="hub-connection-detail"',
                   'id="hub-connection-retry"'):
        assert marker in html, f"в base.html нет {marker}"
    assert "Связь с Hub потеряна" in html
    assert "Повторить" in html
    # Устаревшее содержимое гасится, иначе «Ожидаем…» выглядит живым.
    assert "hub-stale" in html


@pytest.mark.parametrize(
    "event",
    ["htmx:sendError", "htmx:timeout", "htmx:responseError", "htmx:afterRequest", "online"],
)
def test_base_template_handles_every_failure_event(event: str) -> None:
    """Обработчик есть для каждого события, которым htmx сообщает о сбое и об успехе."""
    html = BASE_TEMPLATE.read_text(encoding="utf-8")
    listener = f"addEventListener('{event}'"
    assert listener in html, f"в base.html нет обработчика {event}"


def test_base_template_does_not_retry_unsafe_requests_silently() -> None:
    """Автоповтор — только для выборки: молча повторять POST/DELETE нельзя."""
    html = BASE_TEMPLATE.read_text(encoding="utf-8")
    assert "verb !== 'get'" in html, "в base.html нет ограничения автоповтора на GET"
