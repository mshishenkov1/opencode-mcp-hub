# Сопоставление встроенного MCP-сервера Mattermost Agents 2.5.1 и `magnit-tag-mcp`

Дата: 2026-08-20. Только исследование, код продукта не менялся.

## Источники

**А — их сервер.** `mattermost/mattermost-plugin-agents`, тег **`v2.5.1`** (коммит `41b285e57aa5f556d8885cd29f4a7be2a313b0af`, релиз от 2026-07-29). Тег найден и соответствует плагину `mattermost-ai` 2.5.1, установленному на стенде, — читались файлы именно с этого ref, не с default branch. Каталог `mcpserver/tools/`, плюс `mcp/meta_tools.go` и `mcpserver/proxy_tools.go`.

**Б — наш сервер.** `/Users/miroslavshishenkov/Documents/magnit-tag-mcp`, ветка `feature/oauth-mode`. Данные получены запуском `uv run python -m tag_mcp --list-tools` и `--coverage`, разбором `tag_mcp/openapi_layer.py` и вендоренного спека `spec/mattermost-openapi-v4.yaml`.

### Важное уточнение по `access_mode.go`

Вопреки ожиданию, `AccessMode` у них — это **не** режим доступа read/write. Файл содержит ровно два значения:

```go
AccessModeLocal  AccessMode = "local"   // есть доступ к локальной ФС
AccessModeRemote AccessMode = "remote"  // работа по сети, ограничения безопасности
```

Режим влияет только на схему аргументов (`NewJSONSchemaForAccessMode`) и на то, доступны ли пути локальной ФС (`upload_file` с локального пути, вложения в `create_post`). **Разделения инструментов на чтение/запись у них нет вообще** — ни тегов, ни аннотаций `readOnlyHint`. Ниже признак read/write проставлен мной по семантике операции. Это, кстати, наше архитектурное преимущество: у нас теги политики (`read` / `write` / `dm` / `destructive` / `admin` / `lite`) заданы явно и управляют видимостью.

## 1. Сводка числами

| Показатель | Значение |
|---|---|
| Инструментов у них, всего в коде (v2.5.1) | **127** |
| — из них dev-only (`create_user`, `create_team`, `create_post_as_user`), видны только при `devMode` | 3 |
| — из них automations (5 шт.), скрыты, если не установлен плагин автоматизаций | 5 |
| **Реально видны обычному пользователю на проде** | **119** |
| Инструментов у нас, курируемых | **66** (62 видны модели, 4 скрыты политикой) |
| Операций OpenAPI в спеке | 582 (из них 172 admin) |
| Операций, «закрытых» курируемым слоем (`COVERED_OPERATION_IDS`) | 61 |
| Операций, номинально доступных автослою (`--coverage`) | 521 |
| **Реально отдаётся автослоем при дефолтных настройках** (`MM_ENABLE_GENERATED=true`, без admin, без destructive) | **281** |
| — при `MM_ALLOW_DESTRUCTIVE=true` | 316 |
| — при `MM_ENABLE_ADMIN=true` + destructive | 473 |
| Их инструментов, покрытых нашим **курируемым** тулом один-в-один | **47** |
| Их инструментов, покрытых **автослоем** (operationId доступен) | **52** |
| Их инструментов, покрытых **частично** (близкий наш тул есть, но уже по функциональности) | **16** |
| Их инструментов, **не покрытых у нас никак** | **12** |

Разбивка сходится: 47 + 52 + 16 + 12 = 127.

Если считать только по 119 «продовым» инструментам (без 3 dev-тулов и 5 automations), то: **47 курируемых + 51 автослоем = 98 покрыто полноценно**, 16 частично, **5 не покрыто вообще** — а именно `list_agents`, `get_users_not_in_channel`, `get_users_not_in_team`, `get_new_users_in_team`, `get_role`.

Замечание про 521 против 281. Цифра 521 из `--coverage` — это «всё, что не дублирует курируемый слой», без учёта политик. Фактическая выдача автослоя режется тремя фильтрами в `tag_mcp/openapi_layer.py`: `ADMIN_TAGS`/`ADMIN_PATH_RE` (172 операции), `DESTRUCTIVE_OPERATION_RE` (`^Delete|Remove|Revoke|Restore|Convert|Disable|…`) и жёсткий `FORBIDDEN_OPERATION_RE`/`NEVER_EXPOSE_OPERATION_IDS` (креды, токены, MFA, перезапуск сервера). В таблице ниже это отмечено метками «+destructive» и «+admin» там, где операция закрыта дефолтом.

## 2. Таблица «есть у них — как покрыто у нас»

Обозначения: **К** — курируемый тул, **А** — автослой (operationId), **Ч** — покрыто частично, **✗** — не покрыто.

### posts (13)

| Их инструмент | r/w | Как покрыто у нас |
|---|---|---|
| `read_post` | read | К `get_post` + `get_thread` |
| `create_post` | write | К `create_post` |
| `dm` | write | К `send_direct_message` |
| `group_message` | write | А `CreateGroupChannel` + `CreatePost` (двумя вызовами) |
| `get_post_info` | read | Ч К `get_post` (канал/команда — не всегда) · А `GetPostInfo` |
| `list_pinned_posts` | read | К `get_pinned_posts` |
| `list_saved_posts` | read | К `get_saved_posts` |
| `update_post` | write | К `update_post` |
| `delete_post` | write | К `delete_post` (скрыт тегом `destructive`) |
| `pin_post` | write | К `pin_post` |
| `unpin_post` | write | К `unpin_post` |
| `save_post` | write | К `save_post` |
| `acknowledge_post` | write | А `SaveAcknowledgementForPost` |

### scheduled_posts (5)

| Их инструмент | r/w | Как покрыто у нас |
|---|---|---|
| `list_scheduled_posts` | read | А `GetUserScheduledPosts` |
| `create_scheduled_post` | write | А `CreateScheduledPost` |
| `update_scheduled_post` | write | А `UpdateScheduledPost` |
| `delete_scheduled_post` | write | А `DeleteScheduledPost` (+destructive) |
| `set_post_reminder` | write | А `SetPostReminder` |

### reactions (6)

| Их инструмент | r/w | Как покрыто у нас |
|---|---|---|
| `get_post_reactions` | read | К `list_reactions` |
| `get_bulk_reactions` | read | А `GetBulkReactions` |
| `list_custom_emoji` | read | К `list_custom_emoji` |
| `search_custom_emoji` | read | К `search_emoji` |
| `add_reaction` | write | К `add_reaction` |
| `remove_reaction` | write | К `remove_reaction` |

### threads / unreads (9)

| Их инструмент | r/w | Как покрыто у нас |
|---|---|---|
| `get_threads` | read | А `GetUserThreads` |
| `get_mentions` | read | Ч К `search_posts` (у них — `SearchPosts` по `@username`, отдельного тула у нас нет) |
| `get_unread_counts` | read | Ч К `get_my_unreads` (по каналам, без сводки по командам) · А `GetTeamsUnreadForUser` |
| `get_channel_unread` | read | А `GetChannelUnread` |
| `get_posts_around_unread` | read | А `GetPostsAroundLastUnread` (наш `get_posts_around` — вокруг поста, не вокруг метки прочтения) |
| `mark_channel_read` | write | К `mark_channel_read` |
| `mark_channels_viewed` | write | Ч К `mark_channel_read` (по одному каналу; bulk нет — `ViewChannel` в `COVERED`, автослой его не отдаёт) |
| `mark_post_unread` | write | А `SetPostUnread` |
| `set_thread_follow` | write | А `StartFollowingThread` / `StopFollowingThread` |

### channels (15)

| Их инструмент | r/w | Как покрыто у нас |
|---|---|---|
| `read_channel` | read | К `get_channel_posts` |
| `create_channel` | write | К `create_channel` |
| `get_channel_info` | read | К `get_channel` / `resolve_channel` |
| `get_channel_members` | read | К `list_channel_members` |
| `add_channel_member` | write | К `add_channel_member` |
| `get_user_channels` | read | К `list_my_channels` |
| `get_channel_stats` | read | К `get_channel_stats` |
| `get_channel_member_counts` | read | А `GetChannelsMemberCount` |
| `search_channels` | read | К `search_channels` |
| `list_team_channels` | read | Ч К `browse_public_channels` (только публичные) · А `GetPrivateChannelsForTeam` |
| `list_archived_channels` | read | А `GetDeletedChannelsForTeam` |
| `update_channel` | write | Ч К `set_channel_header`, `set_channel_purpose`; **переименование (`display_name`/`name`) — ✗** (`PatchChannel` в `COVERED`, автослой его не отдаёт) |
| `archive_channel` | write | К `archive_channel` (скрыт `destructive`) |
| `restore_channel` | write | А `RestoreChannel` (+destructive) |
| `convert_channel_privacy` | write | А `UpdateChannelPrivacy` |

### channel_members (12)

| Их инструмент | r/w | Как покрыто у нас |
|---|---|---|
| `get_channel_member` | read | А `GetChannelMember` |
| `get_channel_members_by_ids` | read | А `GetChannelMembersByIds` |
| `get_channel_members_by_status` | read | Ч К `list_users(in_channel=…)` (без сортировки по присутствию) |
| `get_user_channel_memberships` | read | А `GetChannelMembersForUser` |
| `get_users_not_in_channel` | read | **✗** (`GetUsers` в `COVERED`, а наш `list_users` не умеет `not_in_channel`) |
| `search_users_in_channel` | read | К `search_users(in_channel=…)`; ветка `not_in` — ✗ |
| `list_sidebar_categories` | read | А `GetSidebarCategoriesForTeamForUser` |
| `add_channel_members` | write | Ч К `add_channel_member` (по одному, bulk нет) |
| `remove_channel_member` | write | К `remove_channel_member` (скрыт `destructive`) |
| `set_channel_mute` | write | А `UpdateChannelNotifyProps` |
| `set_channel_favorite` | write | Ч К `set_my_preference` (нужно знать категорию `favorite_channel`) |
| `update_channel_notify_props` | write | А `UpdateChannelNotifyProps` |

### bookmarks (4)

| Их инструмент | r/w | Как покрыто у нас |
|---|---|---|
| `list_channel_bookmarks` | read | А `ListChannelBookmarksForChannel` |
| `create_channel_bookmark` | write | А `CreateChannelBookmark` |
| `update_channel_bookmark` | write | А `UpdateChannelBookmark` |
| `delete_channel_bookmark` | write | А `DeleteChannelBookmark` (+destructive) |

### users (10)

| Их инструмент | r/w | Как покрыто у нас |
|---|---|---|
| `get_me` | read | К `whoami` / `get_my_profile` |
| `get_user` | read | К `get_user` |
| `get_user_by_username` | read | К `get_user` (принимает логин / `@логин`) |
| `get_user_by_email` | read | К `get_user` (принимает почту) |
| `get_users_by_ids` | read | Ч К `get_user` / `list_users` (батч по списку id — нет) |
| `get_users_by_usernames` | read | А `GetUsersByUsernames` |
| `get_user_stats` | read | А `GetTotalUsersStats` |
| `get_user_cpa_values` | read | А `ListCPAValues` |
| `list_cpa_fields` | read | А `ListAllCPAFields` |
| `update_user` | write | А `PatchUser` / `UpdateUser` |

### status (5)

| Их инструмент | r/w | Как покрыто у нас |
|---|---|---|
| `get_user_status` | read | К `get_user_statuses` |
| `get_users_statuses` | read | К `get_user_statuses` |
| `get_user_custom_status` | read | Ч К `get_user` (кастомный статус в `props`) |
| `set_status` | write | К `set_my_status` |
| `set_dnd` | write | Ч К `set_my_status("dnd")` (без `end_time`; `UpdateUserStatus` в `COVERED`) |

### teams (17)

| Их инструмент | r/w | Как покрыто у нас |
|---|---|---|
| `get_team_info` | read | К `get_team` |
| `get_team_members` | read | К `list_team_members` |
| `add_team_member` | write | А `AddTeamMember` |
| `get_team_member` | read | А `GetTeamMember` |
| `get_team_stats` | read | А `GetTeamStats` |
| `get_user_teams` | read | Ч К `list_my_teams` (для себя; для другого — `GetTeamsForUser` в `COVERED`) |
| `get_users_in_team` | read | К `list_users(team=…)` |
| `get_users_not_in_team` | read | **✗** (`GetUsers?not_in_team` — параметра нет ни у нас, ни в автослое) |
| `get_new_users_in_team` | read | **✗** (`GetUsers?sort=create_at`) |
| `get_dm_common_teams` | read | А `GetGroupMessageMembersCommonTeams` |
| `search_teams` | read | К `search_teams` |
| `search_users_in_team` | read | К `search_users(team=…)` |
| `add_team_members` | write | А `AddTeamMembers` |
| `remove_team_member` | write | А `RemoveTeamMember` (+destructive) |
| `update_team` | write | А `PatchTeam` |
| `invite_users_to_team` | write | А `InviteUsersToTeam` |
| `invite_users_to_team_and_channels` | write | Ч А `InviteUsersToTeam` (без привязки к каналам) |

### search (2)

| Их инструмент | r/w | Как покрыто у нас |
|---|---|---|
| `search_posts` | read | Ч К `search_posts` — **у них гибрид: семантический (RAG/эмбеддинги) + keyword, у нас только keyword** |
| `search_users` | read | К `search_users` |

### files (6)

| Их инструмент | r/w | Как покрыто у нас |
|---|---|---|
| `read_file` | read | Ч К `download_file` (сохраняет на диск и отдаёт путь; текст вложения в ответ не извлекается) |
| `get_file_info` | read | К `get_file_info` |
| `get_post_files` | read | А `GetFileInfosForPost` |
| `get_file_link` | write | К `get_file_public_link` |
| `search_files` | read | К `search_files` |
| `upload_file` | write | К `upload_file` |

### integrations (4)

| Их инструмент | r/w | Как покрыто у нас |
|---|---|---|
| `get_bot` | read | А `GetBot` |
| `list_bots` | read | К `list_bots` |
| `list_incoming_webhooks` | read | К `list_incoming_webhooks` |
| `list_outgoing_webhooks` | read | К `list_outgoing_webhooks` |

### groups (6)

| Их инструмент | r/w | Как покрыто у нас |
|---|---|---|
| `get_group_info` | read | А `GetGroup` |
| `list_groups` | read | А `GetGroups` |
| `get_user_groups` | read | А `GetGroupsByUserId` |
| `get_channel_groups` | read | А `GetGroupsByChannel` |
| `get_team_groups` | read | А `GetGroupsByTeam` |
| `get_users_in_group_channels` | read | А `GetUsersByGroupChannelIds` |

### roles (4)

| Их инструмент | r/w | Как покрыто у нас |
|---|---|---|
| `get_role` | read | **✗** при дефолте — `GetRole`/`GetRoleByName` под тегом `roles` ⇒ отсечены `ADMIN_TAGS`; доступны только при `MM_ENABLE_ADMIN=true` |
| `get_channel_moderations` | read | А `GetChannelModerations` |
| `update_channel_member_roles` | write | А `UpdateChannelRoles` |
| `update_team_member_roles` | write | А `UpdateTeamMemberRoles` |

### agents / automations / dev (9)

| Их инструмент | r/w | Как покрыто у нас |
|---|---|---|
| `list_agents` | read | **✗** — плагинный эндпоинт `/plugins/mattermost-ai/ai_bots`, не публичный API |
| `list_automations` | read | **✗** — плагин автоматизаций |
| `get_automation_instructions` | read | **✗** — плагин автоматизаций |
| `create_automation` | write | **✗** — плагин автоматизаций |
| `update_automation` | write | **✗** — плагин автоматизаций |
| `delete_automation` | write | **✗** — плагин автоматизаций |
| `create_user` (dev) | write | **✗ и не нужно** — `CreateUser` в нашем `FORBIDDEN_OPERATION_RE` навсегда |
| `create_post_as_user` (dev) | write | **✗ и не нужно** — требует логина по паролю, у нас запрещено жёстко |
| `create_team` (dev) | write | А `CreateTeam` |

### Мета-инструменты (`mcp/meta_tools.go`, `mcpserver/proxy_tools.go`)

Отдельно от 127 доменных тулов у них есть слой динамической загрузки инструментов: **`search_tools`** (поиск подходящего MCP-тула по запросу) и **`load_tool`** (загрузка тула по точному имени; до загрузки вызов возвращает подсказку «tool is available but not loaded»). `proxy_tools.go` поднимает агрегатор `mattermost-agents-plugin-aggregator`, который проксирует внешние MCP-серверы под общим неймспейсом.

Это прямой аналог нашей проблемы «много тулов — модель хуже выбирает», решённый другим способом: они держат 119 тулов, но подгружают их лениво. У нас та же задача решена профилями (`lite` и теги политики). **У нас такого механизма нет — и это самая интересная идея для заимствования, но на уровне Hub/каталога, а не самого `magnit-tag-mcp`.**

## 3. Чего нет у нас — с приоритетом и оценкой трудозатрат

Список отсортирован по убыванию пользы. Помню про принцип: раздувать список курируемых тулов вредно, поэтому в «высокий» попало только то, что либо закрывает частый сценарий, либо чинит дырку в уже заявленной функциональности.

| # | Что | Приоритет | Трудозатраты | Почему |
|---|---|---|---|---|
| 1 | **Семантический поиск в `search_posts`** (у них — гибрид эмбеддингов и keyword) | Высокий | **L** | Самое заметное функциональное отставание. Пользователи спрашивают «где обсуждали X», а keyword-поиск Mattermost по русскому морфологически слаб. Требует индекса эмбеддингов и внешнего сервиса — отсюда L. Можно вынести отдельным MCP, а не тулом внутри `magnit-tag-mcp`. |
| 2 | **Переименование канала** — `display_name` / `name` в `update_channel` | Высокий | **S** | Сейчас дырка: `set_channel_header`/`set_channel_purpose` есть, а переименовать нельзя, потому что `PatchChannel` заперт в `COVERED_OPERATION_IDS`. Правится расширением существующего тула (`rename_channel` или параметры в общий `update_channel`), новых тулов почти не добавляет. |
| 3 | **Отложенные сообщения** — `create_scheduled_post` / `list_scheduled_posts` / `delete_scheduled_post` | Высокий | **M** | Формально покрыто автослоем, но это ровно тот случай, когда курируемая обёртка окупается: удобные `channel`/время в человеческом виде, валидация «время в будущем». Частый корпоративный сценарий («напиши завтра в 9»). 2–3 тула, не больше. |
| 4 | **Тред-инбокс и упоминания** — `get_threads`, `get_mentions` | Высокий | **M** | Закрывает сценарий «что я пропустил» лучше, чем текущий `get_my_unreads`: CRT-треды и непрочитанные упоминания. `get_mentions` у них — обёртка над `SearchPosts` по `@username`, дёшево. Логично слить в один расширенный `get_my_unreads(include_threads=…, include_mentions=…)`, а не плодить тулы. |
| 5 | **`read_file` — извлечение текста вложения** | Средний | **M** | Наш `download_file` кладёт файл на диск, но модель в удалённом режиме (HTTP/OAuth) к диску не имеет доступа. Их `read_file` возвращает текст. Нужен разбор txt/md/csv/docx/pdf — отсюда M. |
| 6 | **`not_in_channel` / `not_in_team` в `list_users`** | Средний | **S** | «Кого ещё позвать в канал» — реальный сценарий bulk-invite. Это параметры к существующему тулу, ноль новых тулов. |
| 7 | **Батч-выборки**: `get_users_by_ids`, `get_channel_member_counts`, `get_bulk_reactions` | Средний | **S** | Экономят раунд-трипы и токены на больших каналах. Тоже параметры/варианты существующих тулов; `GetBulkReactions` и `GetChannelsMemberCount` уже доступны автослоем — можно ограничиться документацией. |
| 8 | **Закладки канала** (`*_channel_bookmark`, 4 шт.) | Средний | **S** | Полностью доступно автослоем. Курируемые обёртки — только если появится живой запрос; иначе +4 тула ради редкого сценария. |
| 9 | **`set_post_reminder`** | Средний | **S** | Полезный личный сценарий «напомни мне про это сообщение». Доступно автослоем; курируемая обёртка — 1 тул, дёшево. |
| 10 | **`get_role` / роли** | Низкий | **S** | Сейчас отсечено `ADMIN_TAGS` (`roles`). Это осознанное решение политики, а не пробел. Открывать только под `MM_ENABLE_ADMIN`. |
| 11 | **Группы (LDAP/custom), 6 тулов** | Низкий | **M** | Все доступны автослоем. Корпоративный сценарий редкий, курировать не стоит. |
| 12 | **`invite_users_to_team_and_channels`** | Низкий | **S** | На стенде эту нишу занимает свой плагин `ru.magnit.bulk-invite` — логичнее интегрировать его, а не копировать их тул. |
| 13 | **`list_agents`, automations (5)** | Низкий | **L** | Завязано на их плагины. Нам не нужно: у нас своя оркестрация в Hub. |
| 14 | **dev-тулы (`create_user`, `create_post_as_user`, `create_team`)** | Не делать | — | `create_user` и вход по паролю у нас в `FORBIDDEN_OPERATION_RE` — это жёсткое ограничение безопасности, не политика. |

**Итог по трудозатратам:** пункты 2, 6, 7 — это S, суммарно день-полтора и **ноль или почти ноль новых тулов** (только новые параметры). Пункты 3, 4, 5 — M, добавят 3–5 тулов. Пункт 1 — L и отдельный сервис.

## 4. Есть у нас, нет у них — наши преимущества

- **Теги политики и профили.** `read` / `write` / `dm` / `destructive` / `admin` / `lite` заданы на каждом туле; 4 тула по умолчанию скрыты (`archive_channel`, `create_incoming_webhook`, `delete_post`, `remove_channel_member`), профиль `lite` даёт 22 тула для слабых моделей. У них — плоские 119 тулов без единой аннотации read/write.
- **Автослой поверх OpenAPI.** 281 операция при дефолтных настройках сверх курируемых, с многослойными фильтрами (`ADMIN_TAGS`, `DESTRUCTIVE_OPERATION_RE`, `FORBIDDEN_OPERATION_RE`, `NEVER_EXPOSE_OPERATION_IDS`). У них покрытие API жёстко зафиксировано в коде: чего нет среди 127 тулов — того нет вообще.
- **Резолверы имён.** `resolve_channel` превращает любую ссылку/пермалинк в карточку канала, `get_team` принимает URL-имя, отображаемое имя или id, `get_user` — логин/`@логин`/почту/id, `get_post_by_permalink` открывает ссылку, которую прислал человек. У них для этого — отдельные тулы на каждый способ (`get_user`, `get_user_by_username`, `get_user_by_email`), то есть три тула вместо одного.
- **`get_permalink`** — построение постоянной ссылки на сообщение, чтобы отдать её человеку. Прямого аналога у них нет.
- **`execute_slash_command` и `list_slash_commands`.** Даёт доступ ко всему, что реализовано плагинами через слэш-команды (в том числе кастомные команды Тега). У них ни `ExecuteCommand`, ни списка команд нет вообще — это, пожалуй, наш самый недооценённый рычаг.
- **`server_health` и `server_info`.** Диагностика соединения (доступность, счётчики запросов и повторов) и публичная конфигурация сервера. У них — ничего подобного.
- **`join_channel` / `leave_channel`.** Вступить и выйти от имени владельца токена. У них есть только `add_channel_member` (добавить кого-то), самоприсоединения нет.
- **`browse_public_channels`** — открытые каналы команды, включая те, где владелец токена не состоит.
- **`get_posts_around`** — соседние сообщения вокруг указанного (контекст разговора). У них есть только `get_posts_around_unread` — вокруг метки прочтения.
- **`get_my_preferences` / `set_my_preference`** как явные тулы.
- **`@untrusted`-разметка** на тулах, возвращающих чужой пользовательский контент (виден в `get_my_unreads`) — защита от prompt injection через содержимое каналов. У них такого маркирования нет.
- **OAuth-режим и HTTP-транспорт** (`docs/HTTP_AUTH.md`, ветка `feature/oauth-mode`) — их сервер живёт внутри плагина и наружу отдаётся на их же условиях.

## 5. Чего нет ни у кого: кастомные методы Тега

Кастомные функции ТЭГ — **реплаи (цитирование), форварды (пересылка сообщений), голосование/опросы, массовые очистки каналов** — отсутствуют и в публичном API Mattermost v4 (их нет ни в одной из 582 операций вендоренного спека), и среди 127 инструментов Agents 2.5.1. Проверено прямым поиском по спеку и по их исходникам: совпадений нет.

**По открытым источникам этих методов не видно.** `mattermost-plugin-agents` — апстрим, он ничего не знает о форке Тега. Чтобы их поддержать, нужен доступ к исходникам плагинов стенда либо к самому форку Mattermost.

### Плагины, установленные на стенде

| ID плагина | Что даёт |
|---|---|
| `ru.magnit.userinfo` | Расширенная карточка сотрудника (кастомные атрибуты) |
| `ru.magnit.bulk-invite` | Массовое приглашение пользователей в каналы/команды |
| `ru.magnit.mattermost-plugin-ews-calendar` | Интеграция с Exchange-календарём |
| `com.opscenter.cards` | Карточки/задачи |
| `com.onlyoffice.mattermost` | Совместное редактирование документов |
| `com.mattermost.calls` | Звонки |

### Способы разведки их REST-эндпоинтов

Что **не** работает: попытка вытащить фронтенд-бандлы плагинов анонимно — `/static/<plugin_id>/…_bundle.js` без авторизации отдаёт **404**, то есть статика плагинов закрыта тем же гейтом, что и API.

Что доступно, по возрастанию усилий:

1. **PAT на `tag-test.corp.tander.ru`.** Единственный дешёвый путь. С валидным токеном:
   - `GET /api/v4/plugins/webapp` — список плагинов с webapp-бандлами и путями к ним;
   - `GET /static/<plugin_id>/<plugin_id>_bundle.js` — сам бандл; из него регулярками вытаскиваются все строки вида `/plugins/<id>/api/...` — это и есть карта REST-эндпоинтов плагина;
   - `GET /api/v4/teams/{team_id}/commands/autocomplete` — слэш-команды плагинов с описаниями аргументов (у нас уже есть курируемый `list_slash_commands`, который это отдаёт);
   - `POST /api/v4/commands/execute` — вызов найденной команды (наш `execute_slash_command`).
2. **DevTools в браузере на живом стенде** — вкладка Network при использовании реплая/форварда/голосования покажет фактические запросы. Быстрее всего для точечной проверки, но не даёт полной карты.
3. **Исходники плагинов** — внутренний GitLab Магнита. Даёт полную и достоверную картину, но требует организационного доступа.

**Рекомендация:** начать с (1). Слэш-команды через уже существующие `list_slash_commands` / `execute_slash_command` могут закрыть часть кастомных сценариев Тега вообще без нового кода — стоит проверить это в первую очередь, до любой разработки.

## 6. Вывод

Функционального разрыва, который бы нас блокировал, нет: 98 из 119 их «продовых» инструментов уже доступны через `magnit-tag-mcp` (48 курируемых, 50 автослоем), и наш автослой на 281 операцию покрывает гораздо больше публичного API, чем их фиксированные 127 тулов. Реально стоит доделать немного и точечно: расширить существующие тулы параметрами (переименование канала — сейчас откровенная дырка; `not_in_channel`/`not_in_team` в `list_users`; батч-выборки) — это S и почти не увеличивает список тулов; затем добавить 3–5 курируемых обёрток для отложенных сообщений и тред-инбокса (`get_threads` + `get_mentions` лучше влить в расширенный `get_my_unreads`, чем плодить тулы) и научить `read_file` возвращать текст вложения, потому что в OAuth/HTTP-режиме диск модели недоступен. Семантический поиск — единственное настоящее отставание, но это отдельный сервис с индексом эмбеддингов, а не тул внутри `magnit-tag-mcp`. Из их сервера через каталог Hub брать имеет смысл не тулы (мы их и так покрываем), а **механизм `search_tools` / `load_tool`**: ленивая подгрузка инструментов — это ровно наша задача «не раздувать список, чтобы модель не путалась», решённая на уровне транспорта, и она хорошо ложится на архитектуру каталога MCP. Кастомные методы Тега (реплаи, форварды, голосование, очистки) по открытым источникам не восстанавливаются — нужен PAT на стенд, а начать разведку стоит со слэш-команд плагинов, которые наши `list_slash_commands` / `execute_slash_command` уже умеют вызывать.
