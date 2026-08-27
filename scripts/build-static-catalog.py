#!/usr/bin/env python3
"""Сборка статического каталога коннекторов: ``catalog.json`` + ``catalog.json.sha256``.

Зачем. Каталог перестал быть свойством Hub (спецификация форка OpenCode, правило **S-C10**,
решение **D-40**): клиент читает его с внутреннего HTTPS-хоста — того же, что раздаёт обновления, —
и для этого ему не нужны ни ключ, ни сессия, ни аудит. Этот скрипт делает из ``catalog.yaml``
ровно то тело, которое клиент получил бы от ``GET {hub}/api/catalog``.

Что здесь принципиально:

* **Формат = конверт Hub.** ``{"version": <int>, "servers": [<карточка>, …]}``, карточка — результат
  ``ServerEntry.public_view`` (R-C6). Второго формата каталога не заводится: у клиента ровно один
  разбор (S-C10 п.4, D-41), и одинаковое тело от Hub и отсюда обязано давать одинаковый результат.
* **Публичное представление берётся у Hub, а не пишется заново.** Карточку строит тот же код
  (``hub.catalog``), поэтому расхождение «Hub отдаёт одно, файл — другое» невозможно по построению.
  Способы подключения (``auth_methods``, R-U8) подхватываются, если эта версия Hub их знает.
* **Утечка секрета — отказ сборки, а не предупреждение.** Поверх публичного представления работает
  проверка запрещённых ключей и значений: ``client_secret``, ``verify``, ``exchange``,
  ``credential_headers``, внутренние ``upstream_url``, ссылки ``env:VAR`` и неподставленные
  ``${VAR}``. Публичный файл раздаётся без авторизации — «почти публичное представление» здесь
  означает утечку, поэтому сборка падает.
* **``${VAR}`` подставляются только в несекретных полях.** Это свойство самого каталога: секреты
  объявляются ссылкой ``env:VAR`` (``auth.client_secret``, ``credential_headers``,
  ``static_headers``), и в публичное представление такие поля не входят вовсе. Сервер с
  неподставленной переменной считается ненастроенным и в файл не попадает — ровно как он не попадает
  в ответ Hub.
* **Словари разрешений** (``catalog/permissions/<alias>.yaml``, правило S-V20) прикладываются к
  карточке полем ``permission_groups`` дословно: клиент разбирает их сам и деградирует на прежний
  экран прав, если формат ему незнаком.

Пример::

    python3 scripts/build-static-catalog.py --public-url https://hub.corp --out dist/catalog.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import yaml

from hub.catalog import ServerEntry, load_catalog
from hub.errors import CatalogError

DEFAULT_CATALOG = "catalog.yaml"
DEFAULT_PERMISSIONS = "catalog/permissions"
DEFAULT_OUT = "dist/catalog.json"

#: Ключи, которых в публичном файле быть не может ни на каком уровне вложенности.
#: ``verify`` и ``exchange`` — блоки способа ``user_token`` (R-U8): первый описывает, каким запросом
#: Hub проверяет введённый токен, второй — как обменивает его на рабочий. Оба содержат внутренние
#: адреса и заголовки и наружу не отдаются даже в усечённом виде.
FORBIDDEN_KEYS = frozenset(
    {
        "client_secret",
        "secret",
        "verify",
        "exchange",
        "credential_headers",
        "static_headers",
        "upstream_url",
        "token_url",
        "revoke_url",
        "audience",
    }
)

#: Ссылка на секрет в переменной окружения (``env:VAR``) — форма, в которой секреты лежат в YAML.
ENV_REF_RE = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")

#: Неподставленная переменная ``${VAR}``: означает, что сервер собран не полностью.
VAR_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


class BuildError(Exception):
    """Ошибка сборки статического каталога: печатается человеку, сборка завершается кодом 2."""


# ---------------------------------------------------------------------------
# Словари разрешений (S-V20)
# ---------------------------------------------------------------------------


def load_permission_groups(directory: Path) -> dict[str, Any]:
    """Прочитать ``<directory>/<alias>.yaml`` для всех alias. Нет каталога — пустой словарь.

    Файл кладётся в карточку **дословно**: клиент разбирает его своими правилами (S-V20) и сам
    решает, что делать с незнакомым форматом. Проверяется здесь ровно одно — что это объект YAML:
    список или строка в поле ``permission_groups`` не деградируют у клиента мягко, а выглядят как
    испорченный каталог.
    """
    if not directory.is_dir():
        return {}
    result: dict[str, Any] = {}
    for path in sorted(directory.glob("*.yaml")):
        alias = path.stem
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise BuildError(f"{path}: ошибка разбора YAML: {exc}") from exc
        if document is None:
            raise BuildError(f"{path}: файл пуст")
        if not isinstance(document, dict):
            raise BuildError(f"{path}: ожидается объект с полями version, groups")
        result[alias] = document
    return result


# ---------------------------------------------------------------------------
# Публичное представление
# ---------------------------------------------------------------------------


def public_card(entry: ServerEntry, public_url: str, permission_groups: dict[str, Any]) -> dict[str, Any]:
    """Карточка каталога для клиента — публичное представление Hub плюс словарь разрешений.

    ``connection`` не добавляется: у статического файла нет пользователя, а состояние карточки
    клиент и так считает по локальным данным (S-C10 п.4, S-V5).
    """
    card: dict[str, Any] = dict(entry.public_view(public_url))
    # R-U8: способы подключения появились в каталоге позже. `getattr` вместо прямого вызова —
    # чтобы скрипт работал и до, и после слияния ветки фасада, а не падал на одной из них.
    methods = getattr(entry, "public_auth_methods", None)
    if callable(methods):
        value = methods()
        if value:
            card["auth_methods"] = value
    groups = permission_groups.get(entry.alias)
    if groups is not None:
        card["permission_groups"] = groups
    return card


def audit(value: Any, path: str = "$") -> list[str]:
    """Найти в готовом теле всё, чего в публичном файле быть не должно.

    Проверка идёт по **результату**, а не по namespace исходника: она ловит и поле, добавленное в
    ``public_view`` завтра, и словарь разрешений, в который кто-то положил лишнее. Возвращается
    список путей — все сразу, чтобы чинить за один заход.
    """
    problems: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{path}.{key}"
            if key in FORBIDDEN_KEYS:
                problems.append(f"{here}: запрещённое поле в публичном каталоге")
                continue
            problems.extend(audit(item, here))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            problems.extend(audit(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        if ENV_REF_RE.match(value):
            problems.append(f"{path}: ссылка на секрет env:VAR")
        elif VAR_RE.search(value):
            problems.append(f"{path}: неподставленная переменная ${{VAR}}")
    return problems


def build(
    *,
    catalog_path: Path,
    permissions_dir: Path,
    public_url: str,
    include_deprecated: bool = True,
    env: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Собрать тело статического каталога. Возвращает ``(документ, строки отчёта)``."""
    catalog = load_catalog(catalog_path, env)
    groups = load_permission_groups(permissions_dir)

    servers: list[dict[str, Any]] = []
    report: list[str] = []
    # Порядок серверов — как в файле (I-1 R-A3): витрина показывает их в этом порядке и ничего не
    # пересортировывает, поэтому порядок здесь — часть контракта, а не деталь реализации.
    for entry in catalog.servers:
        if entry.unconfigured:
            # Тот же критерий, по которому сервер не виден и через Hub: без переменных окружения
            # карточка неполна, а неполная карточка хуже отсутствующей — клиент подключил бы её в
            # никуда. Причину называем: молчаливо исчезнувший коннектор ищут часами.
            report.append(
                f"пропущен {entry.alias}: не заданы переменные {', '.join(entry.missing_vars)}"
            )
            continue
        if "all" not in entry.model.audience:
            # Файл раздаётся без авторизации, поэтому в него попадает только то, что и так видно
            # всем. Ограниченная аудитория остаётся за Hub, где есть кому проверять группы.
            report.append(
                f"пропущен {entry.alias}: аудитория {', '.join(entry.model.audience)} — не 'all'"
            )
            continue
        if not include_deprecated and entry.status == "deprecated":
            report.append(f"пропущен {entry.alias}: статус deprecated")
            continue
        servers.append(public_card(entry, public_url, groups))

    document = {"version": catalog.version, "servers": servers}

    problems = audit(document)
    if problems:
        raise BuildError(
            "публичный каталог не собран — в теле остались непубличные данные:\n  "
            + "\n  ".join(problems)
        )

    unused = sorted(set(groups) - {s["alias"] for s in servers})
    for alias in unused:
        report.append(f"словарь разрешений {alias}.yaml не приложен: такого сервера в каталоге нет")
    return document, report


# ---------------------------------------------------------------------------
# Запись
# ---------------------------------------------------------------------------


def render(document: dict[str, Any]) -> str:
    """Текст файла: UTF-8 без экранирования, отступ 2, перевод строки в конце.

    Кириллица пишется как есть: файл читают и люди — при разборе жалоб на витрину его открывают
    глазами. Порядок ключей — порядок построения, поэтому одинаковый вход даёт побайтово одинаковый
    выход, и sha256 меняется только вместе с содержимым.
    """
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def write(out: Path, text: str) -> str:
    """Записать ``catalog.json`` и ``catalog.json.sha256``; вернуть sha256."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    # Формат строки — как у `sha256sum`: файл рядом обязан проверяться `sha256sum -c` без правки.
    out.with_name(out.name + ".sha256").write_text(f"{digest}  {out.name}\n", encoding="utf-8")
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Собрать статический каталог коннекторов (S-C10) из catalog.yaml",
    )
    parser.add_argument("--catalog", default=DEFAULT_CATALOG, help=f"файл каталога (по умолчанию {DEFAULT_CATALOG})")
    parser.add_argument(
        "--permissions",
        default=DEFAULT_PERMISSIONS,
        help=f"каталог словарей разрешений (по умолчанию {DEFAULT_PERMISSIONS})",
    )
    parser.add_argument(
        "--public-url",
        default=os.environ.get("HUB_PUBLIC_URL", ""),
        help="публичный адрес Hub: из него строится mcp_url коннекторов mode=facade",
    )
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"файл результата (по умолчанию {DEFAULT_OUT})")
    parser.add_argument(
        "--no-deprecated",
        action="store_true",
        help="не включать серверы со статусом deprecated (по умолчанию включаются: у витрины есть бейдж)",
    )
    args = parser.parse_args(argv)

    public_url = args.public_url.rstrip("/")
    if not public_url:
        # Без адреса Hub у facade-карточек получился бы mcp_url вида "/mcp/<alias>" — ссылка в
        # никуда. Пустое значение отвергается здесь, а не всплывает у пользователя.
        print(
            "не задан --public-url (или HUB_PUBLIC_URL): из него строится mcp_url коннекторов mode=facade",
            file=sys.stderr,
        )
        return 2

    try:
        document, report = build(
            catalog_path=Path(args.catalog),
            permissions_dir=Path(args.permissions),
            public_url=public_url,
            include_deprecated=not args.no_deprecated,
        )
        text = render(document)
        out = Path(args.out)
        digest = write(out, text)
    except (BuildError, CatalogError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    for line in report:
        print(line, file=sys.stderr)
    print(f"{out}: version={document['version']}, серверов {len(document['servers'])}")
    print(f"sha256 {digest}")
    return 0


if __name__ == "__main__":  # pragma: no cover - точка входа
    raise SystemExit(main())
