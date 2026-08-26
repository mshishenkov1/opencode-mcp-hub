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


# --- разбор скрипта плашки -------------------------------------------------
#
# Проверки ниже разбирают сам скрипт, а не ищут подстроки: ревью показало, что подстрочный
# поиск не падает на осмысленной поломке — guard снимают из кода, а строку оставляют в
# комментарии, и тест остаётся зелёным. Поэтому комментарии и строковые литералы вырезаются,
# тела функций и обработчиков выделяются по скобкам, и утверждается, ГДЕ стоит проверка и
# какие пути её проходят.

SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.DOTALL)


def _strip_comments(js: str) -> str:
    """Убрать комментарии, не тронув строковые литералы (в них бывают «//» и «/*»)."""
    out: list[str] = []
    i, n = 0, len(js)
    quote: str | None = None
    while i < n:
        ch = js[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(js[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if js.startswith("//", i):
            end = js.find("\n", i)
            i = n if end == -1 else end
            continue
        if js.startswith("/*", i):
            end = js.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _connection_script() -> str:
    """Скрипт обработки сбоев из base.html — без комментариев."""
    html = BASE_TEMPLATE.read_text(encoding="utf-8")
    blocks = [b for b in SCRIPT_RE.findall(html) if "hub-connection-retry" in b]
    assert len(blocks) == 1, "ожидается ровно один скрипт обработки сбоев"
    return _strip_comments(blocks[0])


def _block_after(js: str, marker: str) -> str:
    """Тело в фигурных скобках, начинающееся после ``marker`` (с учётом вложенности)."""
    at = js.find(marker)
    assert at != -1, f"в скрипте нет {marker!r}"
    open_at = js.find("{", at)
    assert open_at != -1, f"после {marker!r} нет тела"
    depth = 0
    for pos in range(open_at, len(js)):
        if js[pos] == "{":
            depth += 1
        elif js[pos] == "}":
            depth -= 1
            if depth == 0:
                return js[open_at + 1 : pos]
    raise AssertionError(f"не закрыто тело после {marker!r}")


def _function_body(js: str, name: str) -> str:
    return _block_after(js, f"function {name}(")


def _handler_body(js: str, event: str) -> str:
    return _block_after(js, f"addEventListener('{event}'")


def _function_params(js: str, name: str) -> list[str]:
    match = re.search(rf"function {re.escape(name)}\(([^)]*)\)", js)
    assert match, f"в скрипте нет функции {name}"
    return [p.strip() for p in match.group(1).split(",") if p.strip()]


# Вызов повтора: имя, открывающая скобка, аргументы до закрывающей.
RETRY_CALL_RE = re.compile(r"\b(retry|retryAll)\(([^)]*)\)")


def _retry_calls(js: str) -> list[tuple[str, str]]:
    """Все вызовы ``retry``/``retryAll`` (без объявлений) как ``(имя, аргументы)``."""
    calls: list[tuple[str, str]] = []
    for match in RETRY_CALL_RE.finditer(js):
        head = js[max(0, match.start() - 9) : match.start()]
        if head.rstrip().endswith("function"):
            continue
        calls.append((match.group(1), match.group(2).strip()))
    return calls


# --- must_fix ревью: автоповтор касается только выборки --------------------


def test_retry_guard_lives_inside_the_single_decision_point() -> None:
    """Проверка метода стоит внутри ``retry`` — до отправки запроса (must_fix ревью i8).

    Раньше проверка стояла в ``scheduleRetry``, а ``retry`` вызывался ещё с двух путей без
    неё; ревью сняло guard из единственного места, оставив строку в комментарии, и набор
    остался зелёным. Здесь утверждается расположение: guard обязан быть в теле ``retry``,
    сравнивать метод с ``get``, зависеть от признака «повтор запрошен человеком» и стоять
    раньше вызова ``htmx.ajax``.
    """
    js = _connection_script()
    params = _function_params(js, "retry")
    assert len(params) >= 2, f"у retry нет признака ручного повтора: {params}"
    user_requested = params[-1]

    body = _function_body(js, "retry")
    guard = re.search(
        rf"if\s*\(\s*!\s*{re.escape(user_requested)}\s*&&\s*\w+\.verb\s*!==\s*'get'\s*\)"
        r"\s*\{\s*return",
        body,
    )
    assert guard, (
        "в теле retry нет проверки «повтор не запрошен человеком и метод не get → выход»; "
        f"тело: {body.strip()[:400]}"
    )
    ajax_at = body.find("htmx.ajax")
    assert ajax_at != -1, "retry не отправляет запрос — проверка вырождена"
    assert guard.start() < ajax_at, "guard стоит после отправки запроса"


def test_only_the_retry_button_may_request_an_unsafe_repeat() -> None:
    """Признак «можно всё» приходит ровно от кнопки; автоматические пути просят безопасный повтор."""
    js = _connection_script()
    calls = _retry_calls(js)
    assert calls, "в скрипте нет вызовов повтора — проверка вырождена"

    truthy = [call for call in calls if call[1].split(",")[-1].strip() == "true"]
    assert len(truthy) == 1, f"признак ручного повтора передаётся не из одного места: {calls}"

    # Единственный «true» обязан быть внутри обработчика нажатия на кнопку плашки.
    ready = _handler_body(js, "DOMContentLoaded")
    assert "hub-connection-retry" in ready
    click = _block_after(ready, "addEventListener('click'")
    assert _retry_calls(click) == [(truthy[0][0], "true")], (
        f"ручной повтор запрашивает не кнопка: {click.strip()!r}"
    )

    # Все прочие вызовы — с явным «false» либо с прокинутым параметром (retryAll → retry).
    passthrough = set(_function_params(js, "retryAll"))
    for name, args in calls:
        last = args.split(",")[-1].strip()
        assert last in {"false", "true", *passthrough}, f"{name}({args}) — непонятный признак"


@pytest.mark.parametrize("event", ["online", "htmx:afterRequest"])
def test_automatic_paths_never_request_an_unsafe_repeat(event: str) -> None:
    """Пути без участия человека (возврат сети, успех соседнего блока) просят только безопасный."""
    js = _connection_script()
    body = _handler_body(js, event)
    calls = _retry_calls(body)
    assert calls, f"обработчик {event} не возобновляет запросы — проверка вырождена"
    for name, args in calls:
        assert args.split(",")[-1].strip() == "false", f"{event}: {name}({args}) повторяет всё"


def test_scheduled_retry_is_automatic_too() -> None:
    """Повтор по таймеру — тоже автоматический путь."""
    js = _connection_script()
    body = _function_body(js, "scheduleRetry")
    calls = _retry_calls(body)
    assert calls, "scheduleRetry ничего не планирует — проверка вырождена"
    for name, args in calls:
        assert args.split(",")[-1].strip() == "false", f"{name}({args}) в таймере повторяет всё"


def test_user_is_told_that_unsafe_request_will_not_repeat_itself() -> None:
    """Обещание самовосстановления даётся только выборке: остальное ждёт человека."""
    js = _connection_script()
    body = _function_body(js, "retryHint")
    assert re.search(r"\w+\.verb\s*===\s*'get'", body), (
        "подсказка не различает выборку и небезопасный запрос"
    )
    assert "сама повторит" in body
    assert "Повторить" in body


# --- обработчики делают дело, а не просто существуют -----------------------


_HANDLERS = [
    ("htmx:sendError", ["connectionLost"]),
    ("htmx:timeout", ["connectionLost"]),
    ("htmx:responseError", ["remember", "showNotice", "scheduleRetry"]),
    # drop(done) обязателен: без него запись об удавшемся блоке остаётся в списке сорванных
    # и её продолжают повторять, а плашка не гаснет.
    ("htmx:afterRequest", ["successful", "stallFor", "drop", "retryAll"]),
    ("online", ["retryAll"]),
]


@pytest.mark.parametrize(("event", "expected"), _HANDLERS, ids=[c[0] for c in _HANDLERS])
def test_failure_handler_body_does_its_job(event: str, expected: list[str]) -> None:
    """Обработчик каждого события не просто зарегистрирован — он делает то, ради чего заведён.

    Прежняя проверка искала подстроку ``addEventListener('<событие>')`` и не различала, что
    внутри: пустой обработчик прошёл бы её.
    """
    js = _connection_script()
    body = _handler_body(js, event)
    assert body.strip(), f"обработчик {event} пуст"
    for call in expected:
        assert call in body, f"обработчик {event} не вызывает {call}: {body.strip()[:300]}"


def test_connection_lost_shows_notice_and_schedules_retry() -> None:
    """Сбой связи всегда показывает плашку и заводит автоповтор (решение о нём — внутри retry)."""
    js = _connection_script()
    body = _function_body(js, "connectionLost")
    for call in ("remember", "showNotice", "retryHint", "scheduleRetry"):
        assert call in body, f"connectionLost не вызывает {call}"


def test_server_error_resumes_only_on_5xx() -> None:
    """4xx сам не пройдёт — автоповтор заводится только на 5xx."""
    js = _connection_script()
    body = _handler_body(js, "htmx:responseError")
    assert re.search(r"status\s*>=\s*500", body), "нет условия на 5xx"
    at = body.find("scheduleRetry")
    assert at != -1
    assert re.search(r"status\s*>=\s*500", body[:at]), "автоповтор заводится независимо от кода"
