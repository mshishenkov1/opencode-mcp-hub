# Отчёт конвейера: I-4 «Корпоративный форк OpenCode — SSO, витрина коннекторов, умолчания, сборки»

Репозиторий `opencode` (форк anomalyco/opencode v1.17.9), ветка `corp/i4-sso-connectors`.

## Реализовано
Корп-слой конфига через build-константы (`OPENCODE_CORP_HUB_URL`/`OPENCODE_CORP_CONFIG`, ванильный режим без них);
вход по корпоративному SSO при первом запуске (TUI и Desktop/web; браузер открывает сервер, код/ссылка — запасной
путь; выбор команды LiteLLM; отмена входа освобождает сессию), `opencode corp login|status` (контракт вывода S-A11a);
витрина `/connectors` (TUI + Desktop/web: карточки из Hub `/api/catalog`+`/api/me/connections`, статусы по S-V6,
подключение = запись `mcp.<alias>` в глобальный конфиг + стандартный MCP-OAuth, отключение тремя локальными шагами,
права/пресеты, кэш каталога 24 ч, «Hub недоступен»); i18n RU/EN (+ словарь TUI); SDK перегенерирован (`corp.*`);
сборка `corp/build.ts` (CLI 4 целей, версия `-magnit.N`; проверена реальная darwin-arm64 сборка), `corp/upstream-sync.sh`,
реестр правок `corp/patches.md` (ровно 19 upstream-файлов), CI `.github/workflows/corp-ci.yml`.

## Спецификация
`corp/docs/spec.md` 1.0 → 1.1 (74 правила; S-A11a; решения D-1…D-15), AC-01…AC-123 — все unit/integration/ci покрыты;
30 ручных процедур (М-1…М-10) в тест-отчёте.

## Тесты
opencode 174 · tui 27 · app 13 (corp-сьюты, дважды, стабильно) + smoke 39/39; базовые upstream-падения не ухудшены.
Мутации: 7/7 (dev-цикл) и 7/7 (review) пойманы.

## Баги и ревью
BUG-I4-001 (medium, grep в CI) — fixed за 1 итерацию. Review-i4-1: request_changes (3 must_fix — статус карточки
после connect, отсутствие тестов оркестрации, автозапуск браузера в Desktop) — закрыты; review-i4-2: approve
(4 minor: 2 спек-уточнения → спек-цикл 1.2, устаревшая колонка patches.md, хрупкий приём в dialog-actions.test).

## Ограничения
Рендер экранов TUI/Electron и живой MCP-OAuth (callback 19876) — ручные процедуры; сборки win/desktop — CI/Windows-стенд;
подпись сборок — решение ИБ (D-7).
