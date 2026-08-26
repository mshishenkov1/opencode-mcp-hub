"""Статика страниц Hub раздаётся самим Hub, без публичных CDN (R-W6): регрессия BUG-I3-004.

Страницы тянули htmx с ``https://unpkg.com``: в закрытом контуре доступ туда не гарантирован,
каждое открытие страницы — исходящее обращение наружу из периметра, а исполняемый сторонний код
не был зафиксирован хешем. Проверяется наблюдаемое: что отдаёт ``GET /static/{name}``, что лежит
в образе (хеши сверяются с ``SHA256SUMS`` — он источник истины) и на что ссылаются страницы.

Все проверки локальные: файлы читаются из дерева, страницы рендерятся приложением. Сети нет.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import pytest

from hub.assets import (
    HTMX_FILE,
    HTMX_JSON_ENC_FILE,
    HTMX_VERSION,
    JS_CONTENT_TYPE,
    STATIC_CACHE_CONTROL,
    STATIC_DIR,
    STATIC_FILES,
)
from tests.conftest import Hub, HubFactory
from tests.support import (
    TAG_ENV,
    add_key,
    asgi_stream,
    catalog_doc,
    mock_start,
    start_body,
    user_token_facade,
    web_login,
)

SUMS_FILE = STATIC_DIR / "SHA256SUMS"
TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "hub" / "templates"

SCRIPT_SRC_RE = re.compile(r"<script[^>]*\ssrc=[\"']([^\"']+)[\"']", re.IGNORECASE)
# Любой внешний адрес: со схемой (http://, https://) либо протокол-относительный (//host/...).
EXTERNAL_SRC_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:)?//", re.IGNORECASE)


async def _hub(make_hub: HubFactory, **overrides: Any) -> Hub:
    hub = await make_hub(
        catalog=catalog_doc([user_token_facade("tag")]),
        env=TAG_ENV,
        base_url="https://hub.test",
        **overrides,
    )
    await add_key(hub, "sk-ok", "u1")
    return hub


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _declared_sums() -> dict[str, str]:
    """``SHA256SUMS`` → ``{имя файла: ожидаемая сумма}`` (формат ``shasum -a 256``)."""
    sums: dict[str, str] = {}
    for line in SUMS_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        sums[name.strip()] = digest.strip()
    return sums


# --- раздача /static/{name} ------------------------------------------------


@pytest.mark.parametrize("name", sorted(STATIC_FILES))
async def test_static_asset_is_served_from_the_image(make_hub: HubFactory, name: str) -> None:
    """BUG-I3-004: файл отдаётся Hub'ом целиком, нужным типом и как неизменяемый."""
    hub = await _hub(make_hub)
    expected = (STATIC_DIR / name).read_bytes()

    response = await hub.get(f"/static/{name}")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == JS_CONTENT_TYPE
    assert response.headers["cache-control"] == STATIC_CACHE_CONTROL
    # Версия зашита в имя файла, поэтому ответ неизменяем и кэшируется надолго.
    assert "immutable" in STATIC_CACHE_CONTROL
    assert response.content == expected
    assert len(response.content) == len(expected)
    assert int(response.headers["content-length"]) == len(expected)


async def test_served_htmx_is_the_real_library_not_a_stub(make_hub: HubFactory) -> None:
    """Отдаётся именно htmx нужного выпуска и расширение json-enc, а не пустышка."""
    hub = await _hub(make_hub)
    htmx = await hub.get(f"/static/{HTMX_FILE}")
    ext = await hub.get(f"/static/{HTMX_JSON_ENC_FILE}")
    assert htmx.status_code == 200 and ext.status_code == 200
    assert len(htmx.content) > 10_000, "htmx.min.js подозрительно мал"
    assert b"htmx" in htmx.content
    # Расширение json-enc не входит в htmx.min.js: без него формы слали urlencoded (BUG-I3-004).
    assert b"json-enc" in ext.content


@pytest.mark.parametrize(
    "name", ["nope.js", "SHA256SUMS", "htmx.min.js", "htmx-1.9.11.min.js", ""]
)
async def test_unknown_static_name_is_404(make_hub: HubFactory, name: str) -> None:
    """Имя вне белого списка не отдаётся: сам список сумм наружу тоже не идёт."""
    hub = await _hub(make_hub)
    response = await hub.get(f"/static/{name}")
    assert response.status_code == 404, f"{name}: {response.status_code}"


# Payload'ы подобраны так, чтобы на наивной реализации (склейка имени с каталогом статики,
# без белого списка) каждый разрешался в **существующий** файл и отдавал его содержимое.
# Слэш в пути маршрут не пропускает сам по себе — значит, «..» и разделители обязаны быть
# закодированы, иначе проверка выродится: она не упадёт и на дырявом обработчике.
_TRAVERSAL = [
    # src/hub/static/../assets.py — исходник рядом с каталогом статики.
    ("..%2fassets.py", b"STATIC_FILES"),
    # src/hub/static/../templates/base.html — шаблон страницы.
    ("..%2ftemplates%2fbase.html", b"<!DOCTYPE html>"),
    # src/hub/static/../../../catalog.yaml — каталог в корне репозитория.
    ("..%2f..%2f..%2fcatalog.yaml", b"servers:"),
    # Те же цели незакодированным путём и смешанным кодированием.
    ("../assets.py", b"STATIC_FILES"),
    ("../templates/base.html", b"<!DOCTYPE html>"),
    ("../../../catalog.yaml", b"servers:"),
    ("%2e%2e%2fassets.py", b"STATIC_FILES"),
    ("..%2F..%2F..%2Fcatalog.yaml", b"servers:"),
]


@pytest.mark.parametrize(
    ("suffix", "leak"), _TRAVERSAL, ids=[case[0] for case in _TRAVERSAL]
)
async def test_path_traversal_never_serves_a_file(
    make_hub: HubFactory, suffix: str, leak: bytes
) -> None:
    """Выход за каталог статики невозможен: имя ищется только в белом списке.

    Запрос идёт прямым вызовом ASGI: httpx нормализовал бы ``..`` ещё в клиенте, и проверка
    выродилась бы — приложение получило бы уже безопасный путь.
    """
    hub = await _hub(make_hub)
    path = f"/static/{suffix}"
    async with asgi_stream(hub.app, "GET", path) as stream:
        body = await stream.read_all()
    assert stream.status_code != 200, f"{path}: отдан файл"
    assert leak not in body, f"{path}: в ответе содержимое файла вне каталога статики"


def test_traversal_payloads_resolve_to_existing_files() -> None:
    """Сторож самой проверки обхода: каждая цель обязана существовать на диске.

    Иначе тест выше «зелёный» просто потому, что файла нет, и уязвимую реализацию он
    пропустит — ровно этим была вырождена прежняя редакция набора.
    """
    from urllib.parse import unquote

    for suffix, _leak in _TRAVERSAL:
        target = (STATIC_DIR / unquote(suffix)).resolve()
        assert target.is_file(), f"{suffix} ведёт в несуществующий {target}"
        assert STATIC_DIR.resolve() not in target.parents, f"{suffix} не выходит за каталог"


# --- целостность файлов в образе -------------------------------------------


def test_static_files_match_sha256sums() -> None:
    """Хеши файлов совпадают с ``SHA256SUMS`` — эквивалент ``shasum -a 256 -c SHA256SUMS``.

    Файл сумм — источник истины: он фиксирует, какой именно сторонний код попадает в образ.
    """
    declared = _declared_sums()
    assert declared, "SHA256SUMS пуст"
    for name, digest in declared.items():
        path = STATIC_DIR / name
        assert path.is_file(), f"в SHA256SUMS есть {name}, а файла нет"
        assert _sha256(path.read_bytes()) == digest, f"{name}: содержимое не совпадает с суммой"


VERSION_IN_NAME_RE = re.compile(r"\d+\.\d+\.\d+")


@pytest.mark.parametrize("name", sorted(STATIC_FILES))
def test_served_file_name_carries_its_version(name: str) -> None:
    """Имя раздаваемого файла обязано нести версию — иначе immutable-кэш залипнет на год.

    ``Cache-Control: immutable`` с ``max-age`` на год допустим ровно потому, что смена версии
    даёт новый адрес. Безверсионное имя в раздаче сделало бы обновление невозможным: у всех,
    кто открыл страницу, осталась бы прежняя копия.
    """
    assert VERSION_IN_NAME_RE.search(name), f"в имени {name} нет версии"
    assert HTMX_VERSION in name, f"{name} не несёт текущую версию {HTMX_VERSION}"
    assert "31536000" in STATIC_CACHE_CONTROL and "immutable" in STATIC_CACHE_CONTROL


def test_every_static_file_is_covered_by_sums_and_whitelist() -> None:
    """Ни одного файла мимо сумм и мимо белого списка: иначе в образ попадёт неучтённый код."""
    declared = set(_declared_sums())
    on_disk = {p.name for p in STATIC_DIR.iterdir() if p.is_file() and p.name != "SHA256SUMS"}
    assert on_disk == declared, "состав каталога статики и SHA256SUMS разошёлся"
    assert set(STATIC_FILES) == declared, "белый список раздачи и SHA256SUMS разошлись"


# --- страницы не ходят наружу ----------------------------------------------


def test_no_template_references_an_external_script() -> None:
    """Ни один шаблон не тянет скрипт со стороннего адреса (R-W6, BUG-I3-004)."""
    templates = sorted(TEMPLATES_DIR.glob("*.html"))
    assert templates, "шаблоны не найдены — проверка вырождена"
    external: list[str] = []
    for path in templates:
        for src in SCRIPT_SRC_RE.findall(path.read_text(encoding="utf-8")):
            if EXTERNAL_SRC_RE.match(src):
                external.append(f"{path.name}: {src}")
    assert not external, f"внешние скрипты в шаблонах: {external}"


def test_no_template_mentions_a_public_cdn() -> None:
    """Адресов публичных CDN в шаблонах нет ни в каком виде."""
    for path in sorted(TEMPLATES_DIR.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for cdn in ("unpkg.com", "cdn.jsdelivr.net", "cdnjs.cloudflare.com", "ajax.googleapis.com"):
            assert cdn not in text, f"{path.name} ссылается на {cdn}"


async def test_rendered_pages_load_scripts_only_from_hub(make_hub: HubFactory) -> None:
    """На каждой странице Hub все скрипты — с самого Hub, и каждый адрес действительно отдаётся."""
    hub = await _hub(make_hub, web_auth="litellm")
    mock_start(hub.litellm, start_body())

    pages = {}
    # Страница входа — до создания веб-сессии: с активной сессией она отвечает редиректом.
    login = await hub.get("/auth/login")
    assert login.status_code == 200, login.text
    pages["/auth/login"] = login.text

    await web_login(hub)
    for path in ("/ui/connections", "/ui/servers/tag"):
        response = await hub.get(path)
        assert response.status_code == 200, f"{path}: {response.text}"
        pages[path] = response.text
    # Страница ошибки строится тем же base.html — она тоже не должна ходить наружу.
    missing = await hub.get("/ui/servers/nope")
    assert missing.status_code == 404, missing.text
    pages["/ui/servers/nope"] = missing.text

    seen: set[str] = set()
    for path, html in pages.items():
        sources = SCRIPT_SRC_RE.findall(html)
        assert sources, f"{path}: на странице нет ни одного <script src> — проверка вырождена"
        for src in sources:
            assert not EXTERNAL_SRC_RE.match(src), f"{path}: внешний скрипт {src}"
            assert src.startswith("/static/"), f"{path}: неожиданный адрес скрипта {src}"
            seen.add(src)

    assert seen == {f"/static/{HTMX_FILE}", f"/static/{HTMX_JSON_ENC_FILE}"}
    for src in sorted(seen):
        asset = await hub.get(src)
        assert asset.status_code == 200, f"{src} со страницы не отдаётся: {asset.status_code}"
