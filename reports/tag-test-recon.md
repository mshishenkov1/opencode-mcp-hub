# Разведка тестового стенда ТЭГ и сквозная проверка MCP

Дата: 2026-08-20
Стенд: `https://tag-test.corp.tander.ru` (тестовый). Боевой `tag.magnit.ru` не затрагивался.
Режим работы: только чтение. Ничего не создавалось, сообщений не отправлялось, настройки не менялись.

Учётка: `shishenkov_ma`, роли `system_user system_user_access_token` (не админ).
Токен: личный access token из `.env`, отпечаток `t851…`, длина 26. В отчёте, логах и коммитах значение не фигурирует.

---

## 1. Окружение и что доступно с правами обычного пользователя

| Параметр | Значение |
|---|---|
| Продукт | Mattermost **11.9.4**, форк «ТЭГ» (BuildEnterpriseReady, сборка 10464 от 2026-08-20) |
| Поисковый бэкенд | OpenSearch (`ActiveSearchBackend: opensearch`) |
| Кластер | включён (`EnableCluster: true`) |
| Локаль по умолчанию | `ru` |
| Команд у пользователя | 1 (публичная), публичных каналов в ней — 34 |
| Webapp-плагинов | 7 |
| Ключей в `config/client` | 316 |

**Доступно обычному пользователю (200):**
`/api/v4/users/me`, `/api/v4/config/client?format=old`, `/api/v4/system/ping`, `/api/v4/plugins/webapp`,
`/api/v4/users/me/teams`, `/api/v4/commands?team_id=…`, `/api/v4/teams/{id}/commands/autocomplete`,
`/api/v4/achievements`, `/api/v4/agents`, `/api/v4/llmservices`, `/api/v4/limits/server`,
`/api/v4/e2ee/pubkey`, плюс весь обычный контентный API (каналы, посты, поиск, файлы).

Отдельно важно: **бандлы webapp-плагинов скачиваются обычным пользователем** по
`/static/plugins/<plugin_id>/<bundle>.js` (не по `bundle_path` из манифеста — там путь без сегмента
`plugins`, он даёт 404). Это и есть основной канал разведки кастомных фич: в бандлах лежит
**полный `Client4` форка**, то есть весь перечень серверных маршрутов.

**Недоступно с правами обычного пользователя (зафиксировано, это норма):**

| Эндпоинт | Код | Причина |
|---|---|---|
| `/api/v4/config` | 403 | нужны права админа |
| `/api/v4/plugins` (список установленных) | 403 | нужны права админа |
| `/api/v4/data_retention/policies` | 403 | нужны права админа |
| `/api/v4/data_retention/policy` | 501 | не покрыто лицензией |
| `/api/v4/recaps` | 501 | функция выключена на стенде |
| `/api/v4/content_flagging/flag/config` | 501 | функция выключена на стенде |
| `/api/v4/polls/{post_id}/details` | 403 | `app.poll.no_permission` — детали чужого голосования не отдаются |

---

## 2. Плагины и их эндпоинты

### 2.1 Установленные webapp-плагины

| ID | Название | Версия | Происхождение |
|---|---|---|---|
| `ru.magnit.userinfo` | Magnit User Info | 1.0.3 | **свой** (Магнит) |
| `ru.magnit.bulk-invite` | Bulk Inviter | 0.1.1 | **свой** (форк upstream) |
| `ru.magnit.mattermost-plugin-ews-calendar` | EWS calendar | 0.0.2 | **свой** (Магнит) |
| `com.opscenter.cards` | OpsCenter Cards | 1.18.8 | сторонний |
| `mattermost-ai` | Agents | 2.5.1 | upstream |
| `com.mattermost.calls` | Calls | 1.11.15 | upstream |
| `com.onlyoffice.mattermost` | ONLYOFFICE | 2.3.0 | upstream |

Исходники своих плагинов лежат во внутреннем GitLab: `coderepo.corp.tander.ru/tag-messenger/…`.

Помимо webapp-плагинов на сервере есть **серверные плагины без фронтенда** — их видно только по
слэш-командам: `autolink`, `welcomebot`, `business_metrics`, `mobile-logs`, `hello`.
Прямой список через `/api/v4/plugins` недоступен (403).

### 2.2 Таблица плагинных эндпоинтов

| Плагин | Путь | Метод | Доступность | Что даёт |
|---|---|---|---|---|
| `ru.magnit.userinfo` | `/plugins/ru.magnit.userinfo/api/v1/user/{user_id}/custom_profile` | GET | **200** | Оргструктура сотрудника: `structureHierarchy[]` (`order`, `displayName`, `value`) — тип подразделения, дирекция, департамент и т.д. Плюс руководитель. Это то, чего нет в штатном профиле MM |
| `ru.magnit.mattermost-plugin-ews-calendar` | `/plugins/…ews-calendar/api/v1/client_config` | GET | **200** | `{allowedShowCalendar: bool, allowedTeamIds: string}` — доступен ли календарь пользователю |
| то же | `/plugins/…ews-calendar/api/v1/event_list?startdate=&enddate=&userTimeZone=` | GET | **500** | Список событий Exchange. На стенде падает (`Failed to get event list`) — вероятно, нет связки с EWS |
| то же | `/plugins/…ews-calendar/api/v1/event/{id}` | GET | не проверялся | Карточка события |
| то же | `/plugins/…ews-calendar/api/v1/event/{id}/meeting_accept/` | POST | не проверялся (пишущий) | Принять встречу |
| `ru.magnit.bulk-invite` | `/plugins/ru.magnit.bulk-invite/handlers/channel_bulk_add/status?channel_id=` | GET | не проверялся | Статус фоновой задачи массового добавления |
| то же | `/plugins/…/handlers/channel_bulk_add` | POST | не проверялся (пишущий) | Массовое добавление в канал (multipart, поля `import_threshold_days` и др.) |
| то же | `/plugins/…/handlers/channel_bulk_add/cancel` | POST | не проверялся (пишущий) | Отмена задачи |
| то же | `/plugins/…/handlers/import_from_channel` | POST | не проверялся (пишущий) | Перенос состава из другого канала (`source_channel_id`, `add_to_team`) |
| `com.opscenter.cards` | `/api/v4/posts/{post_id}/actions/{action_id}` | POST | не проверялся (пишущий) | Своего API у плагина нет: рисует пост-тип `custom_opscenter_card` и жмёт штатные interactive-actions |
| `mattermost-ai` | `/plugins/mattermost-ai/post/{post_id}`, `/channel/{id}`, `/summarize-channel` | GET/POST | не проверялся | Саммари треда/канала. Управляется через `/api/v4/agents` |

**Вывод по плагинам:** единственный по-настоящему ценный *читающий* плагинный эндпоинт —
`userinfo/custom_profile` (оргструктура). Остальное либо пишущее, либо сломано на стенде,
либо дублирует штатный API.

### 2.3 Кастомные эндпоинты самого форка (не плагины!)

Главная находка разведки. Из `Client4` форка (извлечён из webapp-чанка `6612.*.js` и из бандла Calls)
видно маршруты, которых **нет в upstream Mattermost**:

| Область | Путь | Метод | Доступность | Что даёт |
|---|---|---|---|---|
| **Голосования** | `/api/v4/polls/{post_id}/details` | GET | **403** (чужой опрос) | Детальные результаты |
| | `/api/v4/polls/{post_id}` | PUT | пишущий | Редактировать опрос |
| | `/api/v4/polls/{post_id}/vote` | POST | пишущий | Проголосовать (`{choices: []}`) |
| | `/api/v4/polls/{post_id}/revoke` | POST | пишущий | Отозвать голос |
| | `/api/v4/polls/{post_id}/close` | POST | пишущий | Закрыть голосование |
| **Достижения** | `/api/v4/achievements?page=&per_page=` | GET | **200** | Каталог достижений (14 шт. на стенде). Поля: `id`, `display_name`, `description`, `type`, `system`, `user_id`, `create_at`, `update_at`, `delete_at` |
| | `/api/v4/achievements/admin?page=&per_page=` | GET | **200** (пусто) | Админский срез |
| | `/api/v4/achievements/grant/{user_id}` | GET | **200** | Какие достижения выданы пользователю |
| | `/api/v4/achievements/favorites/user/{user_id}` | GET | **200** | Избранные достижения пользователя |
| | `/api/v4/achievements/grant` | POST | пишущий | Выдать достижение |
| | `/api/v4/achievements/grant/{a}/{b}` | DELETE | пишущий | Отозвать |
| | `/api/v4/achievements/favorites/{id}` | POST/DELETE | пишущий | Добавить/убрать из избранного |
| | `/api/v4/achievements/favorites` | PUT | пишущий | Переупорядочить |
| | `/api/v4/achievements` | POST | пишущий | Создать достижение |
| **Форварды** | `/api/v4/posts/forward` | POST | пишущий | Массовая пересылка постов |
| **Статусы прочтения** | `/api/v4/read_statuses/{post_id}` | PUT | пишущий | Отметки «прочитано» по конкретному сообщению |
| **E2EE** | `/api/v4/e2ee/pubkey` | GET | **200** (`null` — ключа нет) | Свой публичный ключ |
| | `/api/v4/e2ee/pubkey` | PUT / DELETE | пишущий | Загрузить / удалить ключ |
| | `/api/v4/e2ee/pubkey/users` | POST | пишущий | Ключи списка пользователей |
| | `/api/v4/e2ee/backup` | GET | **404** (`no_pub_keys`) | Бэкап ключей |
| **Настройки канала** | `/api/v4/teams/name/{team}/channels/name/{ch}/preferences/{key}` | GET | 404 (нет записи) | Кастомные преференции канала |
| **Диагностика** | `/api/v4/client_stall` | POST | пишущий | Телеметрия зависаний клиента |

Прочее из форка, включённое на стенде: `/api/v4/agents` (200, 1 агент), `/api/v4/llmservices` (200),
`/api/v4/recaps` (501 — выключено).

---

## 3. Слэш-команды

`GET /api/v4/commands?team_id=…` → **37 команд**; `GET /api/v4/teams/{id}/commands/autocomplete` → те же 37
с деревом автодополнения. Все с пустым `plugin_id` и пустым `url` — то есть это встроенные
и плагинные команды, зарегистрированные сервером, а не вебхук-интеграции.

**Плагинные / нестандартные (6 с деревом подкоманд):**

| Команда | Подкоманды | Что покрывает |
|---|---|---|
| `/autolink` | add, delete, disable, enable, list, set, test | Автолинковка по regex (номера задач → ссылки) |
| `/welcomebot` | set/get/delete_channel_welcome, set/get/delete_team_welcome | Приветственные сообщения |
| `/business_metrics` | add_downtime, list_downtime, help | **Учёт простоев сервисов** — потенциально интересно для дежурств |
| `/call` | start, join, leave, link, stats, end, logs, recording, host | Звонки |
| `/secure-connection` | create, accept, remove, status | Межинстансные соединения |
| `/share-channel` | invite, uninvite, unshare, status | Общие каналы |
| `/mobile-logs` | `[on\|off\|status] [@username]` | Сбор логов мобильного клиента |
| `/hello` | `[@username]` | Демо-плагин |

Остальные 29 — штатные Mattermost (`/msg`, `/join`, `/search`, `/dnd`, `/status`, `/invite`, `/header` и т.д.).

**Голосования слэш-командой НЕ создаются** — `/poll` на стенде отсутствует. Это отличие от
типовых инсталляций Mattermost с `matterpoll`: в ТЭГ опросы вшиты в ядро.

**Доступность через наши инструменты:** `list_slash_commands` виден и работает.
`execute_slash_command` помечен тегом `write` и в `MM_WRITE_MODE=readonly` скрыт — то есть
дешёвый путь «сделать кастомную фичу через слэш-команду» **упирается не в отсутствие
инструмента, а в режим записи**. В `MM_WRITE_MODE=full` он доступен.

---

## 4. Фичи, которые называл тимлид

### 4.1 Голосование — **есть, серверное, не плагин**

`EnablePolls: true`, `MaxPollOptions`, тип поста `poll`. Найден реальный опрос на стенде.

Структура поста (штатный `GET /api/v4/posts/{id}`, доступен обычному пользователю):

```
type: "poll"
message: "<текст сопроводительного сообщения>"
props.poll_config: { question, options[], allow_multiple: bool, anonymous: bool }
metadata.poll: { post_id, started_at, ended_at, total_votes,
                 option_counts[], last_vote_at, current_user_voted }
```

Читать опросы **можно уже сейчас** через обычные пост-эндпоинты — отдельного read-API не нужно.
`GET /api/v4/polls/{id}/details` даёт только 403 для чужих опросов, то есть это не тот путь.

### 4.2 Форварды — **есть, серверные**

`EnableForwards: true`, `MaxPostsPerForward` (по умолчанию 10), `ForwardPreserveInReplyTo: true`.
API: `POST /api/v4/posts/forward`. В webapp есть режим выделения нескольких постов
(`START_FORWARDING_MODE` / `STOP_FORWARDING_MODE`) — то есть это массовая пересылка
выделенных сообщений в другой канал, а не штатный «Forward» одного поста.

### 4.3 «Очистки» — **как отдельной фичи не найдено**

Проверено целенаправленно: в 259 webapp-чанках (22 МБ), 7 бандлах плагинов и по всему
маршрутному дереву `Client4` нет ни одного маршрута, действия Redux, i18n-ключа или UI-компонента
со смыслом «очистка / cleanup / purge / clear history». Все совпадения по `cleanup` — внутренности
сторонних библиотек (react-beautiful-dnd, pdf.js).

Наиболее вероятная трактовка термина — **Data Retention** (политики автоудаления сообщений и файлов):

- `DataRetentionEnableMessageDeletion: false`, `DataRetentionEnableFileDeletion: false`
- `DataRetentionMessageRetentionHours: 8760` (365 дней), то же для файлов
- `/api/v4/data_retention/policies` → **403**, `/api/v4/data_retention/policy` → **501** (лицензия)

То есть на тестовом стенде очистки **выключены и недоступны обычному пользователю**.
Уточнить у тимлида, что именно имелось в виду: retention-политики, ручная очистка канала
или что-то ещё — по коду клиента третьего варианта не существует.

### 4.4 Бонусом найдено (тимлид не называл)

- **Достижения** (`EnableAchievements: true`) — полноценная геймификация с каталогом,
  выдачей и избранным. Читающие эндпоинты доступны обычным пользователем.
- **E2EE** (`EnableE2EE: true`, лимит участников канала 10, макс. файл 50 МБ).
- **Burn on read** — фича-флаг есть (`FeatureFlagBurnOnRead: true`), но выключена
  (`EnableBurnOnRead: false`); TTL от 600 с до 7 суток.
- **Голосовые сообщения** (`EnableVoiceMessages: true`), **кросс-командный поиск**
  (`EnableCrossTeamSearch: true`), **ABAC** (`EnableAttributeBasedAccessControl: true`).

---

## 5. Сквозная проверка MCP против тестового стенда

Репозиторий `magnit-tag-mcp`, ветка `feature/oauth-mode`, рабочее дерево чистое, файлы не менялись.

Запуск:

```
MM_HTTP_AUTH=passthrough MM_WRITE_MODE=readonly \
MM_URL=https://tag-test.corp.tander.ru \
uv run python -m tag_mcp --http --host 127.0.0.1 --port 8933
```

Транспорт streamable HTTP, эндпоинт `http://127.0.0.1:8933/mcp`, авторизация — штатный
`Authorization: Bearer <личный токен>` (кастомного заголовка нет, verifier — `tag_mcp/httpauth.py`).

### Результаты

| Показатель | Значение |
|---|---|
| `initialize` | `magnit-tag` **0.3.0**, протокол `2025-11-25`, 191 мс |
| `tools/list` | **40 инструментов**, все читающие, 13 мс |
| Всего тулов в слое | 66 → 40 видно, **26 скрыто политикой** (20 `write`, 3 `destructive`, 1 `admin`, 2 по обоим) |
| Per-user авторизация | **работает**: `whoami` → `shishenkov_ma`, `is_system_admin: false`, общий `MM_TOKEN` процесса не используется, клиент создаётся per-token через `ClientPool` |
| HTTP-статусы за сессию | 31×200 + 5×202 на `POST /mcp`, 5×200 `GET`, 5×200 `DELETE`. **Ни одного 5xx** |
| Токен в логах | только sha256-отпечаток (16 hex), проверено грепом |

Тайминги вызовов (все читающие):

| Инструмент | Результат | Время |
|---|---|---|
| `whoami` | `shishenkov_ma`, 1 команда, `write_mode: readonly` | 257 мс |
| `list_my_teams` | 1 команда | 7 мс |
| `list_my_channels` | 3 канала (2 direct, 1 public) | 160–179 мс |
| `browse_public_channels` | 34 канала | 62 мс |
| `search_posts "тест"` | 1 результат | 410 мс |
| `search_posts "встреча"` | 0 результатов | 190 мс |
| `get_my_unreads` | пусто | 111–173 мс |
| `list_users` | 20 записей | 63 мс |
| `server_info` | Mattermost 11.9.4 | 42 мс |
| `server_health` | healthy, requests=8, retries=0, failures=0 | 188 мс |
| `create_post` (негативная) | отказ политикой, сообщение **не отправлено** | 10 мс |

Холодный вызов 150–260 мс, повторные — единицы миллисекунд за счёт кэша резолва.
Сервер остановлен, порт 8933 освобождён (проверено `lsof` и `pgrep`).

### Найденные проблемы

1. **TLS: первый запуск упал с 401.** Стенд отдаёт сертификат корпоративного CA, которого нет
   в bundle `certifi`; `curl` работает, потому что берёт корни из keychain macOS.
   Диагностика вводит в заблуждение — код `upstream_unavailable` и хинт «проверь доступность
   **tag.magnit.ru**» захардкожен на боевой хост и не отражает `MM_URL`.
   В `tag_mcp/config.py` `verify_ssl` — только `bool`, пути к CA-бандлу нет, хотя `httpx` его принимает.
   Штатный выход сейчас — `MM_VERIFY_SSL=false`, что хуже, чем свой CA-бандл.
   Обход для проверки: `SSL_CERT_FILE` с выгруженными из keychain корнями, проверка TLS не отключалась.

2. **`list_my_channels` всегда возвращает `has_more: true`.**
   `tag_mcp/tools/channels.py:54` строит страницу как `ChannelPage.build(channels, page=0, per_page=len(channels))`,
   а `tag_mcp/models.py:307` считает `has_more = len(channels) >= per_page > 0` — условие истинно всегда,
   кроме пустого списка. Модель будет думать, что часть каналов скрыта, и пойдёт за несуществующей страницей 1.

3. **`PostSummary` теряет `props` и `metadata` (кроме `files`/`reactions`).**
   `tag_mcp/models.py:180–240`. Практическое следствие: пост голосования доезжает как
   `type="poll"` с сопроводительным текстом, но **без вопроса, вариантов и результатов** —
   они целиком лежат в `props.poll_config` и `metadata.poll`. То же касается пост-типов
   `custom_opscenter_card` и агентских постов.

4. **Нулевая осведомлённость о кастомных фичах ТЭГ.** Грепом по `tag_mcp/`:
   ни одного упоминания polls / achievements / forwards / read_statuses / e2ee
   (единственные совпадения — `_forward_signal` в `supervisor.py` и `forward_pkce` в OAuth).

---

## 6. Выводы и приоритет

### Что закрывается уже сейчас существующими инструментами

- **Чтение голосований** — `get_post` / `get_channel_posts` / `get_thread` уже ходят по нужным
  эндпоинтам. Нужна не новая интеграция, а **проброс `props` и `metadata.poll` в `PostSummary`**.
  Это правка одной модели, а не новый тул. Самое дешёвое улучшение с самым заметным эффектом.
- **Слэш-команды** — `list_slash_commands` работает; `execute_slash_command` существует и
  открывает `/autolink`, `/welcomebot`, `/business_metrics`, `/mobile-logs` без единой строки нового кода.
  Ограничение чисто конфигурационное (`MM_WRITE_MODE`).
- **Оргструктура сотрудника** — доступна одним GET к плагину `userinfo`, ответ маленький и стабильный.

### Что требует новых инструментов

| Приоритет | Что | Обоснование |
|---|---|---|
| **1** | Проброс `props`/`metadata` в `PostSummary` (+ разбор `poll_config`/`metadata.poll`) | Голосования — фича №1 у тимлида. Данные уже приходят, мы их выбрасываем. Минимальная правка |
| **2** | Починить `has_more` в `list_my_channels` | Активно вредит: модель уходит за несуществующей страницей |
| **3** | Поддержка корпоративного CA (`MM_CA_BUNDLE`) + честный хинт в диагностике | Блокирует запуск на любом корпоративном стенде; сейчас лечится только отключением TLS-проверки |
| **4** | `get_user_org_profile` поверх `userinfo/custom_profile` | Даёт то, чего нет в штатном API: дирекция, департамент, руководитель. Дёшево, один GET |
| **5** | Достижения: `list_achievements`, `get_user_achievements` | Читающие эндпоинты доступны и стабильны. Ценность средняя (геймификация), стоимость низкая |
| **6** | Пишущие: `vote_in_poll`, `forward_posts` | Только после решения по `MM_WRITE_MODE`; форварды к тому же массовые и легко «стреляют в ногу» |
| — | Календарь EWS | **Не браться сейчас**: `event_list` на стенде отдаёт 500, интеграция не работает |
| — | Data retention / «очистки» | **Заблокировано**: 403/501, нужны админ и лицензия. Сначала уточнить у тимлида, что имелось в виду |

### Три главных вывода

1. **Кастом ТЭГ — это в основном не плагины, а форк ядра.** Голосования, форварды, достижения,
   E2EE, read-statuses живут в `/api/v4/*` самого сервера. Разведка через `/api/v4/plugins/webapp`
   даёт лишь часть картины; полный список маршрутов извлекается из `Client4` внутри webapp-чанков —
   этот приём стоит зафиксировать как штатный для будущих ревизий.
2. **Самая ценная фича (голосования) уже почти покрыта** — упирается в одну строку модели,
   которая отбрасывает `props`/`metadata`. Не новый MCP, а багфикс.
3. **MCP работоспособен на тестовом стенде**: 40 инструментов, per-user passthrough-авторизация
   подтверждена, readonly-политика корректно скрывает 26 пишущих тулов и блокирует запись,
   тайминги 150–410 мс, ни одного 5xx. Главный операционный риск — не функционал, а TLS
   с корпоративным CA.
