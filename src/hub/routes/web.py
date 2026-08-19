"""Страницы Hub: вход (OIDC / CLI-SSO), «Мои подключения», карточка сервера (R-W1..R-W6)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from hub.crypto import random_token
from hub.db import Connection, User, to_naive_utc
from hub.errors import HubError
from hub.oidc import OIDCError, user_id_from_claims
from hub.web import (
    current_session,
    group_definitions,
    group_titles,
    html_error,
    login_redirect,
    preset_title,
    safe_next,
    status_title,
)
from hub.websession import CSRF_FIELD, CSRF_HEADER, check_csrf, session_token

router = APIRouter(tags=["web"])
logger = logging.getLogger("hub.web")

OIDC_STATE_PREFIX = "oidcstate:"
WEB_LOGIN_PREFIX = "weblogin:"
WEB_LOGIN_CLIENT = "hub-web"


async def _user_groups(request: Request, user_id: str) -> list[str]:
    db = request.app.state.db
    await db.init()
    async with db.session() as session:
        user = await session.get(User, user_id)
        if user is None or not user.groups:
            return ["all"]
        return list(user.groups)


async def _upsert_user(request: Request, user_id: str, email: str | None) -> None:
    state = request.app.state
    await state.db.init()
    now = to_naive_utc(state.clock.now())
    async with state.db.session() as session, session.begin():
        user = await session.get(User, user_id)
        if user is None:
            session.add(
                User(user_id=user_id, email=email, groups=["all"], created_at=now, updated_at=now)
            )
        else:
            if email:
                user.email = email
            if not user.groups:
                user.groups = ["all"]
            user.updated_at = now


async def _start_web_session(
    request: Request, user_id: str, auth_method: str, next_url: str
) -> RedirectResponse:
    state = request.app.state
    token, csrf = await state.web_sessions.create(user_id, auth_method)
    response = RedirectResponse(next_url, status_code=303)
    state.web_sessions.set_cookies(response, token, csrf)
    response.headers["HX-Redirect"] = next_url
    await state.db.audit(
        "web_login", user_id=user_id, details={"auth_method": auth_method}, ts=state.clock.now()
    )
    logger.info("web_login", extra={"user_id": user_id, "auth_method": auth_method})
    return response


# ---------------------------------------------------------------------------
# Вход
# ---------------------------------------------------------------------------


@router.get("/auth/login")
async def auth_login(request: Request) -> Response:
    state = request.app.state
    next_url = safe_next(request.query_params.get("next"))
    info = await current_session(request)
    if info is not None:
        return RedirectResponse(next_url, status_code=302)
    if state.settings.web_auth == "keycloak":
        return await _login_keycloak(request, next_url)
    return await _login_litellm(request, next_url)


async def _login_keycloak(request: Request, next_url: str) -> Response:
    state = request.app.state
    oidc_state = random_token()
    nonce = random_token()
    verifier = random_token()
    try:
        url = await state.oidc.authorize_url(
            redirect_uri=f"{state.settings.public_url}/auth/callback",
            state=oidc_state,
            nonce=nonce,
            code_verifier=verifier,
        )
    except OIDCError as exc:
        logger.warning("oidc_metadata_failed", extra={"reason": str(exc)})
        return html_error(
            state.templates,
            error="oidc_unavailable",
            message="Провайдер входа недоступен, повторите попытку позже",
            status_code=502,
        )
    await state.kv.set(
        OIDC_STATE_PREFIX + oidc_state,
        {"nonce": nonce, "verifier": verifier, "next": next_url},
        ttl=state.settings.oauth_tx_ttl,
    )
    return RedirectResponse(url, status_code=302)


async def _login_litellm(request: Request, next_url: str) -> Response:
    state = request.app.state
    try:
        started = await state.login.start(WEB_LOGIN_CLIENT)
    except HubError as exc:
        return html_error(
            state.templates,
            error=exc.error,
            message=exc.message or "Не удалось начать вход",
            status_code=exc.status_code,
            retry_url=f"/auth/login?next={next_url}",
        )
    login_id = str(started["login_id"])
    await state.kv.set(
        WEB_LOGIN_PREFIX + login_id,
        {"poll_secret": started["poll_secret"], "next": next_url},
        ttl=state.settings.login_session_ttl,
    )
    return state.templates.page(
        "login.html",
        browser_url=started.get("browser_url"),
        user_code=started.get("user_code"),
        poll_url=f"/auth/login/poll/{login_id}",
        next=next_url,
    )


def _login_fragment(
    request: Request, *, poll: bool = False, teams: list[dict[str, Any]] | None = None,
    error: str | None = None, login_id: str = ""
) -> Response:
    state = request.app.state
    return state.templates.page(
        "login_status.html",
        poll=poll,
        teams=teams or [],
        error=error,
        poll_url=f"/auth/login/poll/{login_id}",
        team_url=f"/auth/login/team/{login_id}",
    )


@router.get("/auth/login/poll/{login_id}")
async def auth_login_poll(login_id: str, request: Request) -> Response:
    """Опрос состояния CLI-SSO из браузера (HTMX) — режим ``HUB_WEB_AUTH=litellm`` (R-W2)."""
    state = request.app.state
    record = await state.kv.get(WEB_LOGIN_PREFIX + login_id)
    if not isinstance(record, dict):
        return _login_fragment(request, error="Сессия входа истекла, начните заново", login_id=login_id)
    try:
        result = await state.login.poll(login_id, str(record.get("poll_secret")))
    except HubError as exc:
        return _login_fragment(
            request, error=exc.message or "Вход не удался", login_id=login_id
        )
    body = result.body
    status = str(body.get("status", ""))
    if status == "team_selection_required":
        return _login_fragment(request, teams=list(body.get("teams") or []), login_id=login_id)
    if status == "pending":
        return _login_fragment(request, poll=True, login_id=login_id)
    if status != "ready":
        return _login_fragment(
            request, error=str(body.get("message") or "Вход не удался"), login_id=login_id
        )
    user = body.get("user") or {}
    user_id = str(user.get("user_id") or "")
    if not user_id:
        return _login_fragment(request, error="Вход не удался", login_id=login_id)
    await state.kv.delete(WEB_LOGIN_PREFIX + login_id)
    return await _start_web_session(request, user_id, "litellm", safe_next(record.get("next")))


@router.post("/auth/login/team/{login_id}")
async def auth_login_team(login_id: str, request: Request) -> Response:
    state = request.app.state
    record = await state.kv.get(WEB_LOGIN_PREFIX + login_id)
    if not isinstance(record, dict):
        return _login_fragment(request, error="Сессия входа истекла, начните заново", login_id=login_id)
    form = await request.form()
    team_id = form.get("team_id")
    try:
        await state.login.choose_team(
            login_id, str(record.get("poll_secret")), {"team_id": str(team_id or "")}
        )
    except HubError as exc:
        return _login_fragment(
            request, error=exc.message or "Не удалось выбрать команду", login_id=login_id
        )
    return _login_fragment(request, poll=True, login_id=login_id)


@router.get("/auth/callback")
async def auth_callback(request: Request) -> Response:
    """Возврат из OIDC: проверка ``state`` и ``id_token``, создание веб-сессии (R-W1)."""
    state = request.app.state
    params = request.query_params
    if params.get("error"):
        return html_error(
            state.templates,
            error="access_denied",
            message="Провайдер входа отклонил запрос: доступ не предоставлен",
        )
    oidc_state = params.get("state")
    record = await state.kv.get(OIDC_STATE_PREFIX + oidc_state) if oidc_state else None
    if not isinstance(record, dict):
        return html_error(
            state.templates,
            error="invalid_state",
            message="Сессия входа не найдена или истекла, начните заново",
        )
    await state.kv.delete(OIDC_STATE_PREFIX + str(oidc_state))
    code = params.get("code")
    if not code:
        return html_error(
            state.templates, error="invalid_request", message="Провайдер входа не передал код"
        )
    try:
        tokens = await state.oidc.exchange_code(
            code=code,
            redirect_uri=f"{state.settings.public_url}/auth/callback",
            code_verifier=str(record.get("verifier", "")),
        )
        claims = await state.oidc.verify_id_token(
            str(tokens["id_token"]), nonce=str(record.get("nonce", ""))
        )
    except OIDCError as exc:
        logger.warning("oidc_login_failed", extra={"reason": str(exc)})
        return html_error(
            state.templates, error="invalid_id_token", message=f"Вход не выполнен: {exc}"
        )
    user_id = user_id_from_claims(claims)
    if not user_id:
        return html_error(
            state.templates,
            error="invalid_id_token",
            message="Вход не выполнен: провайдер не передал идентификатор пользователя",
        )
    email = claims.get("email")
    await _upsert_user(request, user_id, str(email) if isinstance(email, str) else None)
    return await _start_web_session(request, user_id, "keycloak", safe_next(record.get("next")))


@router.post("/auth/logout")
async def auth_logout(request: Request) -> Response:
    state = request.app.state
    info = await current_session(request)
    if info is None:
        return RedirectResponse("/auth/login", status_code=302)
    form = await request.form()
    provided = request.headers.get(CSRF_HEADER) or form.get(CSRF_FIELD)
    check_csrf(info, str(provided) if provided is not None else None)
    await state.web_sessions.delete(session_token(request))
    response = RedirectResponse("/auth/login", status_code=302)
    state.web_sessions.clear_cookies(response)
    response.headers["HX-Redirect"] = "/auth/login"
    return response


# ---------------------------------------------------------------------------
# Страницы пользователя
# ---------------------------------------------------------------------------


@router.get("/ui/connections")
async def ui_connections(request: Request) -> Response:
    state = request.app.state
    info = await current_session(request)
    if info is None:
        return login_redirect("/ui/connections")
    groups = await _user_groups(request, info.user_id)
    await state.db.init()
    async with state.db.session() as session:
        rows = (
            await session.execute(select(Connection).where(Connection.user_id == info.user_id))
        ).scalars()
        connections = {c.alias: c for c in rows}
    items = []
    for entry in state.catalog.visible_for(groups):
        conn = connections.get(entry.alias)
        items.append(
            {
                "alias": entry.alias,
                "title": entry.model.title,
                "description": entry.model.description,
                "status": conn.status if conn else "not_connected",
                "status_title": status_title(conn.status if conn else None),
                "preset_title": preset_title(conn.preset if conn else None),
                "group_titles": group_titles(entry, list(conn.groups or [])) if conn else [],
            }
        )
    return state.templates.page("connections.html", items=items, user_id=info.user_id)


@router.get("/ui/servers/{alias}")
async def ui_server(alias: str, request: Request) -> Response:
    state = request.app.state
    info = await current_session(request)
    if info is None:
        return login_redirect(f"/ui/servers/{alias}")
    groups = await _user_groups(request, info.user_id)
    entry = state.catalog.get(alias)
    if entry is None or entry.unconfigured or not entry.is_visible_to(groups):
        return html_error(
            state.templates, error="not_found", message="Сервер не найден", status_code=404
        )
    await state.db.init()
    async with state.db.session() as session:
        conn = (
            await session.execute(
                select(Connection)
                .where(Connection.user_id == info.user_id, Connection.alias == alias)
                .limit(1)
            )
        ).scalar_one_or_none()
    always, selectable = group_definitions(entry)
    view = entry.public_view(state.settings.public_url)
    view.pop("permission_model", None)
    return state.templates.page(
        "server.html",
        server=view,
        status_title=status_title(conn.status if conn else None),
        needs_reauth_reason=conn.needs_reauth_reason if conn else None,
        preset=(conn.preset if conn else None) or "readonly",
        selected=set(conn.groups or []) if conn else set(),
        always_groups=always,
        groups=selectable,
    )
