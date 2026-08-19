"""Фикстуры: приложение Hub через httpx ``ASGITransport`` + ``asgi-lifespan``, моки LiteLLM (respx),
временные каталоги, ``ManualClock``, чистое окружение ``HUB_*``."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from hub.app import create_app
from hub.clock import ManualClock
from hub.settings import Settings
from tests.support import (
    FERNET_KEY,
    LITELLM_URL,
    POLL_SECRET_HEADER,
    PUBLIC_URL,
    MockNetwork,
    default_catalog,
    litellm_http_client,
    make_litellm_router,
    write_catalog,
)

START_TIME = datetime(2026, 8, 19, 12, 30, 0, tzinfo=UTC)


@pytest.fixture(scope="session", autouse=True)
def _detach_hub_json_handler_between_runs() -> Iterator[None]:
    """Снять JSON-хендлер Hub с root-логгера до и после сессии.

    ``configure_logging`` идемпотентен: хендлер живёт на root-логгере всё время процесса и держит
    поток ``sys.stderr``, захваченный в момент создания. При двух прогонах pytest в одном процессе
    (так работает ``mutmut``: сбор статистики, затем чистый прогон) поток первого прогона уже закрыт,
    и любая запись в лог падает ``ValueError: I/O operation on closed file``. Снимаем хендлер на
    границах сессии — следующий прогон создаст его заново поверх актуального ``sys.stderr``.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_hub_json", False):
            root.removeHandler(handler)
    yield
    for handler in list(root.handlers):
        if getattr(handler, "_hub_json", False):
            root.removeHandler(handler)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """R-N4/AC-146: тесты с маркером ``load`` исключаются из обычного прогона.

    Нагрузочный smoke запускается явно: ``pytest -m load``. Тесты не пропускаются (skip),
    а исключаются из выборки — в отчёте прогона нет ни skip, ни xfail.
    """
    if config.getoption("-m"):
        return
    selected, deselected = [], []
    for item in items:
        (deselected if item.get_closest_marker("load") else selected).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


@pytest.fixture(autouse=True)
def clean_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Тесты не должны зависеть от HUB_* окружения машины."""
    for name in list(os.environ):
        if name.upper().startswith("HUB_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def hub_logs_captured(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="hub")


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START_TIME)


@pytest.fixture
def litellm() -> Iterator[respx.MockRouter]:
    router = make_litellm_router()
    yield router
    router.reset()


@pytest.fixture
def net() -> MockNetwork:
    """Моки внешних систем I-3: upstream MCP, AS целевых систем, OIDC (без сети)."""
    return MockNetwork()


@pytest.fixture
def catalog_path(tmp_path: Path) -> Path:
    """Файл каталога по умолчанию (facade 'gitlab' + native 'tag')."""
    return write_catalog(tmp_path / "catalog.yaml", default_catalog())


def base_settings_kwargs(catalog_path: Path | str, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "public_url": PUBLIC_URL,
        "litellm_base_url": LITELLM_URL,
        "secret_key": "S3CR3T-HUB",
        "encryption_key": FERNET_KEY,
        "catalog_path": str(catalog_path),
        "database_url": "sqlite+aiosqlite:///:memory:",
    }
    kwargs.update(overrides)
    return kwargs


@pytest.fixture
def make_settings(catalog_path: Path) -> Callable[..., Settings]:
    def factory(**overrides: Any) -> Settings:
        return Settings(**base_settings_kwargs(catalog_path, **overrides))

    return factory


@dataclass
class Hub:
    """Запущенное приложение и всё нужное для тестов."""

    app: FastAPI
    client: httpx.AsyncClient
    settings: Settings
    clock: ManualClock
    litellm: respx.MockRouter
    catalog_path: Path
    net: MockNetwork | None = None
    base_url: str = "http://hub.test"

    @property
    def upstream(self) -> Any:
        assert self.net is not None
        return self.net.upstream

    @property
    def provider(self) -> Any:
        assert self.net is not None
        return self.net.provider

    @property
    def oidc(self) -> Any:
        assert self.net is not None
        return self.net.oidc

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.client.get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.client.post(url, **kwargs)

    async def poll(self, login_id: str, secret: str | None) -> httpx.Response:
        headers = {POLL_SECRET_HEADER: secret} if secret is not None else {}
        return await self.client.get(f"/cli/poll/{login_id}", headers=headers)

    async def choose_team(self, login_id: str, secret: str | None, body: Any) -> httpx.Response:
        headers = {POLL_SECRET_HEADER: secret} if secret is not None else {}
        if isinstance(body, str | bytes):
            return await self.client.post(
                f"/cli/poll/{login_id}/team",
                content=body,
                headers={**headers, "Content-Type": "application/json"},
            )
        return await self.client.post(f"/cli/poll/{login_id}/team", json=body, headers=headers)


HubFactory = Callable[..., Awaitable[Hub]]


@pytest.fixture
async def make_hub(
    tmp_path: Path,
    clock: ManualClock,
    litellm: respx.MockRouter,
    catalog_path: Path,
    net: MockNetwork,
) -> AsyncIterator[HubFactory]:
    """Фабрика приложений: ``await make_hub(catalog=..., env=..., **settings_overrides)``.

    * ``catalog`` — dict/str документ каталога (по умолчанию — ``default_catalog()``), либо ``None``,
      чтобы использовать уже существующий ``catalog_path``;
    * ``settings`` — готовый объект ``Settings`` (тогда overrides игнорируются);
    * ``env`` — окружение для ``${VAR}``/``env:VAR`` каталога (``catalog_env``); ``None`` → ``os.environ``;
    * ``kv`` — свой KeyValueStore.
    """
    stack = AsyncExitStack()

    async def factory(
        *,
        catalog: dict[str, Any] | str | None = "default",
        settings: Settings | None = None,
        env: dict[str, str] | None = None,
        kv: Any = None,
        path: Path | None = None,
        base_url: str = "http://hub.test",
        network: MockNetwork | None = None,
        **overrides: Any,
    ) -> Hub:
        cat_path = path or catalog_path
        if catalog == "default":
            write_catalog(cat_path, default_catalog())
        elif catalog is not None:
            write_catalog(cat_path, catalog)
        if settings is None:
            settings = Settings(**base_settings_kwargs(cat_path, **overrides))
        http = litellm_http_client(litellm)
        network = network if network is not None else net
        outbound = network.client()
        app = create_app(
            settings,
            litellm_client=http,
            http_client=outbound,
            clock=clock,
            kv=kv,
            catalog_env=env,
        )
        await stack.enter_async_context(LifespanManager(app))
        client = await stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)
        )
        stack.push_async_callback(http.aclose)
        stack.push_async_callback(outbound.aclose)
        return Hub(
            app=app,
            client=client,
            settings=settings,
            clock=clock,
            litellm=litellm,
            catalog_path=cat_path,
            net=network,
            base_url=base_url,
        )

    yield factory
    await stack.aclose()


@pytest.fixture
async def hub(make_hub: HubFactory) -> Hub:
    return await make_hub()
