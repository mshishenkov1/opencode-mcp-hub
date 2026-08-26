"""Статические ресурсы страниц Hub: htmx лежит в образе и раздаётся самим Hub (R-W6).

Публичные CDN не используются: в закрытом корпоративном контуре страницы должны работать
без исходящих обращений из браузера пользователя наружу. Версия зафиксирована в
:data:`HTMX_VERSION` и в имени файла, хеши файлов — в ``src/hub/static/SHA256SUMS``
(проверка: ``shasum -a 256 -c SHA256SUMS`` в этом каталоге).

Файлы попадают в образ через ``[tool.setuptools.package-data]`` (``hub = [... "static/*"]``)
и ``COPY src ./src`` в ``deploy/Dockerfile.hub``: ни сборка, ни рантайм ничего не тянут из сети.
"""

from __future__ import annotations

from pathlib import Path

HTMX_VERSION = "1.9.12"

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_PREFIX = "/static"

JS_CONTENT_TYPE = "application/javascript; charset=utf-8"
# Версия зашита в имя файла, поэтому ответ неизменяем и кэшируется надолго.
STATIC_CACHE_CONTROL = "public, max-age=31536000, immutable"

HTMX_FILE = f"htmx-{HTMX_VERSION}.min.js"
# Расширение json-enc не входит в htmx.min.js, а формы страниц отправляют JSON (R-U4).
HTMX_JSON_ENC_FILE = f"htmx-ext-json-enc-{HTMX_VERSION}.js"

# Белый список: имя из пути запроса ищется только здесь, выход за каталог невозможен.
STATIC_FILES: dict[str, str] = {
    HTMX_FILE: JS_CONTENT_TYPE,
    HTMX_JSON_ENC_FILE: JS_CONTENT_TYPE,
}


def static_url(name: str) -> str:
    """Адрес ресурса на самом Hub."""
    return f"{STATIC_PREFIX}/{name}"


def read_static(name: str) -> bytes:
    """Содержимое ресурса из образа; ``name`` — только ключ :data:`STATIC_FILES`."""
    return (STATIC_DIR / name).read_bytes()


__all__ = [
    "HTMX_FILE",
    "HTMX_JSON_ENC_FILE",
    "HTMX_VERSION",
    "JS_CONTENT_TYPE",
    "STATIC_CACHE_CONTROL",
    "STATIC_DIR",
    "STATIC_FILES",
    "STATIC_PREFIX",
    "read_static",
    "static_url",
]
