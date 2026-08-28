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
  проверка запрещённых ключей и значений: ``client_secret``, ``upstream_url``, ``expiry``,
  ``permission_model.header``, ссылки ``env:VAR`` и неподставленные ``${VAR}``. Публичный файл
  раздаётся без авторизации — «почти публичное представление» здесь означает утечку, поэтому
  сборка падает.
* **Граница проходит по типу способа подключения, а не по имени поля (R-U8.1).** Прямой режим
  (ревизия 1.13 форка, S-V24) невозможен без описания цели, поэтому у **доступного** способа
  ``type: user_token`` наружу идут блоки ``verify`` и ``exchange`` (вместе с вложенным ``revoke``),
  а у карточки с таким способом — блок ``upstream`` (``url``, ``credential_headers``,
  ``static_headers``). У способа ``oauth2`` те же имена запрещены дословно как прежде: рядом с ними
  лежат ``client_secret`` и ссылки ``env:VAR``. Состав каждого открытого блока проверяется по
  **перечню разрешённого**: поле, добавленное в ``public_view`` завтра, останавливает сборку, а не
  утекает молча (так же не пройдут ``exchange.list`` и ``expiry``).
* **``${VAR}`` подставляются только в несекретных полях.** Это свойство самого каталога: секреты
  объявляются ссылкой ``env:VAR`` (``auth.client_secret``, ``credential_headers``,
  ``static_headers``), и в публичное представление такие поля не входят вовсе. Сервер с
  неподставленной переменной считается ненастроенным и в файл не попадает — ровно как он не попадает
  в ответ Hub. Подстановка выполняется **до** проверки схемы, поэтому в открытых блоках заголовок,
  записанный как ``"${GW_SECRET}"``, к моменту публикации неотличим от литерала: его ловит отдельная
  проверка — значение переменной окружения, названной в каталоге, в заголовке или теле запроса
  запрещено (R-U8.1 п. 6 разрешает подстановку только в четырёх адресах).
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
#: ``verify`` и ``exchange`` — блоки способа подключения (R-U8): первый описывает, каким запросом
#: проверяется введённый токен, второй — как он обменивается на рабочий. У способа ``oauth2`` рядом
#: с ними лежат ``client_secret`` и ссылки ``env:VAR``, поэтому запрет по имени остаётся дословно
#: прежним; исключение делается **только** для мест, перечисленных в ``_boundary`` — блоков
#: доступного способа ``type: user_token`` и блока ``upstream`` его карточки (R-U8.1 п. 2 и п. 4).
#: ``expiry`` (R-U18) и внутренний ``upstream_url`` наружу не идут ни при каком типе способа
#: (R-U8.1 п. 5): адрес цели публикуется как ``upstream.url``, а не этим ключом.
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
        "expiry",
    }
)

#: Ключи, запрещённые не везде, а на своём месте: ``(родительский ключ, ключ)``. Имя заголовка
#: групп разрешений (R-C6) наружу не идёт, но само слово ``header`` слишком общее, чтобы запрещать
#: его на любой глубине: в словарях разрешений (S-V20) оно кладётся дословно.
FORBIDDEN_AT = frozenset({("permission_model", "header")})

#: Обратный случай: имя из ``FORBIDDEN_KEYS``, которое на своём месте означает не секрет, а признак.
#: В составе ``field`` способа ``user_token`` (R-U8) ``secret: true`` — указание приложению
#: маскировать ввод; поле публиковалось и до ревизии 4.4, и ревизия его не трогает. Послабление
#: намеренно узкое: только непосредственно в ``field`` и только для булева значения — строка с этим
#: именем остаётся запрещённой везде, включая сам ``field``.
ALLOWED_FLAGS_AT = frozenset({("field", "secret")})

#: Полный состав блоков, открытых ревизией 4.4 (R-U8.1 п. 2 и п. 4). Это перечень **разрешённого**,
#: а не запрещённого: решение 120 отвергло «публиковать всё, кроме перечисленного» именно потому,
#: что забытое поле утекало бы молча. Ключ, которого здесь нет, останавливает сборку — так ловятся
#: и ``exchange.list`` (R-U15.3), и любое поле, добавленное в ``public_view`` завтра.
VERIFY_KEYS = frozenset(
    {"url", "method", "headers", "expect_status", "account_field", "require_account"}
)
EXCHANGE_KEYS = frozenset(
    {
        "url",
        "method",
        "headers",
        "body",
        "expect_status",
        "token_field",
        "token_id_field",
        "description",
        "revoke",
    }
)
REVOKE_KEYS = frozenset({"url", "method", "headers", "body", "expect_status"})
UPSTREAM_KEYS = frozenset({"url", "credential_headers", "static_headers"})

#: Ссылка на секрет в переменной окружения (``env:VAR``) — форма, в которой секреты лежат в YAML.
ENV_REF_RE = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")

#: Неподставленная переменная ``${VAR}``: означает, что сервер собран не полностью.
VAR_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")

#: Имена переменных, названных в каталоге: ``${VAR}`` и ``env:VAR``. По ним берутся значения,
#: которым в заголовках и телах запросов быть нельзя (R-U8.1 п. 6).
VAR_NAME_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
ENV_NAME_RE = re.compile(r"env:([A-Za-z_][A-Za-z0-9_]*)")


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


def _unknown_keys(block: Any, allowed: frozenset[str], where: str) -> list[str]:
    """Ключи открытого блока, которых нет в перечне разрешённого (R-U8.1 п. 2, п. 4, п. 5)."""
    if not isinstance(block, dict):
        return []
    return [
        f"{where}.{key}: поле не входит в состав публикуемого блока"
        for key in block
        if key not in allowed
    ]


def _boundary(document: Any) -> tuple[set[tuple[Any, ...]], set[tuple[Any, ...]], list[str]]:
    """Где запрет по имени поля снимается (R-U8.1), и что там дополнительно проверяется.

    Возвращает три вещи:

    * ``permitted`` — точные пути, на которых запрещённое имя законно: ``verify`` и ``exchange``
      у **доступного** способа ``type: user_token`` и ``credential_headers``/``static_headers``
      внутри блока ``upstream`` его карточки. Граница по типу способа, а не по имени (решение 120):
      у ``oauth2`` и у недоступного ``user_token`` (R-U1, решение 73) те же имена остаются
      запрещёнными, и путь в перечень не попадает.
    * ``sensitive`` — наборы «имя → строка», открытые ревизией 4.4: заголовки и тела запросов.
      Значения в них обязаны быть дословными (R-U8.1 п. 6), поэтому именно там ищется значение
      переменной окружения. Четыре адреса (``upstream.url``, ``verify.url``, ``exchange.url``,
      ``exchange.revoke.url``) сюда не входят: подстановка в них разрешена, без неё прямого режима
      не существует.
    * ``problems`` — нарушения состава: лишний ключ в открытом блоке и блок ``upstream`` у карточки,
      которая доступного способа ``user_token`` не объявила (R-U8.1 п. 4).
    """
    permitted: set[tuple[Any, ...]] = set()
    sensitive: set[tuple[Any, ...]] = set()
    problems: list[str] = []

    servers = document.get("servers") if isinstance(document, dict) else None
    if not isinstance(servers, list):
        return permitted, sensitive, problems

    for i, server in enumerate(servers):
        if not isinstance(server, dict):
            continue
        base: tuple[Any, ...] = ("servers", i)
        raw_methods = server.get("auth_methods")
        methods = raw_methods if isinstance(raw_methods, list) else []
        direct = False

        for j, method in enumerate(methods):
            if not isinstance(method, dict):
                continue
            # R-U8.1 п. 1 и п. 3: открывается только доступный способ типа user_token.
            if method.get("type") != "user_token" or method.get("available") is not True:
                continue
            direct = True
            mpath = (*base, "auth_methods", j)
            mwhere = f"$.servers[{i}].auth_methods[{j}]"
            permitted.add((*mpath, "verify"))
            permitted.add((*mpath, "exchange"))

            verify = method.get("verify")
            if isinstance(verify, dict):
                problems.extend(_unknown_keys(verify, VERIFY_KEYS, f"{mwhere}.verify"))
                sensitive.add((*mpath, "verify", "headers"))

            exchange = method.get("exchange")
            if isinstance(exchange, dict):
                problems.extend(_unknown_keys(exchange, EXCHANGE_KEYS, f"{mwhere}.exchange"))
                sensitive.add((*mpath, "exchange", "headers"))
                sensitive.add((*mpath, "exchange", "body"))
                revoke = exchange.get("revoke")
                if isinstance(revoke, dict):
                    problems.extend(_unknown_keys(revoke, REVOKE_KEYS, f"{mwhere}.exchange.revoke"))
                    sensitive.add((*mpath, "exchange", "revoke", "headers"))
                    sensitive.add((*mpath, "exchange", "revoke", "body"))

        upstream = server.get("upstream")
        if upstream is None:
            continue
        if not direct:
            # R-U8.1 п. 4: адрес цели и шаблоны заголовков отдаются только карточке, которая
            # объявила доступный способ user_token. Иначе это утечка внутреннего адреса.
            problems.append(
                f"$.servers[{i}].upstream: блок upstream у карточки без доступного "
                "способа user_token"
            )
            continue
        permitted.add((*base, "upstream", "credential_headers"))
        permitted.add((*base, "upstream", "static_headers"))
        sensitive.add((*base, "upstream", "credential_headers"))
        sensitive.add((*base, "upstream", "static_headers"))
        problems.extend(_unknown_keys(upstream, UPSTREAM_KEYS, f"$.servers[{i}].upstream"))

    return permitted, sensitive, problems


def _walk(
    value: Any,
    path: str,
    parts: tuple[Any, ...],
    permitted: set[tuple[Any, ...]],
    sensitive: set[tuple[Any, ...]],
    secret_values: frozenset[str],
) -> list[str]:
    """Обход готового тела: запрещённые имена, ссылки на секреты и подставленные значения."""
    problems: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{path}.{key}"
            hp = (*parts, key)
            flag = isinstance(item, bool) and bool(parts) and (parts[-1], key) in ALLOWED_FLAGS_AT
            if key in FORBIDDEN_KEYS and hp not in permitted and not flag:
                problems.append(f"{here}: запрещённое поле в публичном каталоге")
                continue
            if parts and (parts[-1], key) in FORBIDDEN_AT:
                problems.append(f"{here}: запрещённое поле в публичном каталоге")
                continue
            problems.extend(_walk(item, here, hp, permitted, sensitive, secret_values))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            problems.extend(
                _walk(item, f"{path}[{index}]", (*parts, index), permitted, sensitive, secret_values)
            )
    elif isinstance(value, str):
        if ENV_REF_RE.match(value):
            problems.append(f"{path}: ссылка на секрет env:VAR")
        elif VAR_RE.search(value):
            problems.append(f"{path}: неподставленная переменная ${{VAR}}")
        elif value in secret_values and parts[:-1] in sensitive:
            # R-U8.1 п. 6: наружу идёт только дословно записанная строка. Подстановка ${VAR}
            # выполняется до валидации схемы, поэтому заголовок "${GW_SECRET}" к этому моменту
            # неотличим от литерала — отличить его можно только по значению переменной.
            problems.append(
                f"{path}: значение переменной окружения в публикуемом заголовке или теле запроса "
                "(дословной строкой это быть не может)"
            )
    return problems


def audit(
    value: Any, path: str = "$", *, secret_values: frozenset[str] = frozenset()
) -> list[str]:
    """Найти в готовом теле всё, чего в публичном файле быть не должно.

    Проверка идёт по **результату**, а не по namespace исходника: она ловит и поле, добавленное в
    ``public_view`` завтра, и словарь разрешений, в который кто-то положил лишнее. Возвращается
    список путей — все сразу, чтобы чинить за один заход.

    ``secret_values`` — значения переменных окружения, названных в каталоге (см.
    ``referenced_env_values``). Без них проверка на подставленный ``${VAR}`` в заголовке не
    работает: отличить подстановку от литерала по самой строке невозможно.
    """
    permitted, sensitive, problems = _boundary(value)
    problems.extend(_walk(value, path, (), permitted, sensitive, secret_values))
    return problems


def referenced_env_values(catalog_path: Path, env: dict[str, str] | None) -> frozenset[str]:
    """Значения переменных, названных в тексте каталога (``${VAR}`` и ``env:VAR``).

    Читается сам файл каталога: имена нужны до и независимо от разбора, а ``$ref`` в нём внутренние
    (``#/servers/...``). Пустые значения отбрасываются — сравнивать с ними бессмысленно.
    """
    environ = os.environ if env is None else env
    try:
        text = catalog_path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    names = set(VAR_NAME_RE.findall(text)) | set(ENV_NAME_RE.findall(text))
    return frozenset(value for value in (environ.get(name) for name in names) if value)


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

    problems = audit(document, secret_values=referenced_env_values(catalog_path, env))
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
