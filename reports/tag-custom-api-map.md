# Полная карта кастомного API ТЭГ относительно ванильного Mattermost

Дата: 2026-08-20. Стенд `https://tag-test.corp.tander.ru` (Mattermost **11.9.4**, форк «ТЭГ», сборка 10464).
Боевой `tag.magnit.ru` не затрагивался. Режим — **только чтение**: GET/HEAD по API и статике,
ни одного POST/PUT/DELETE к данным (единственные POST — `posts/search` для проверки
кросс-командного поиска, он не меняет состояние).

Учётка `shishenkov_ma`, роли `system_user system_user_access_token`. Токен из `.env`,
отпечаток `t851…`, длина 26 — значение нигде не печаталось и в отчёт не попало.
Персональных данных коллег в отчёте нет: приведены только имена полей, не значения.

Продолжение разведки из `tag-test-recon.md`. Прошлый заход дал ~35 эндпоинтов «на глаз»;
здесь тот же материал разобран машинно и вычтена ванильная база.

---

## 1. Метод и охват

### Как считалось

1. **Сбор фронтенда.** С `/` снят `index.html`, из него три точки входа
   (`main.<hash>.js`, `manifest.js`, `remote_entry.js`). Из рантайма webpack (`__webpack_require__.u`)
   вынута карта `chunkId → hash` — **259 чанков**; скачано 258 (один, `3.acd44f7f…`, отдаёт 404 —
   мёртвая ссылка в карте). Итого **261 файл, 22 МБ** JS.
   Отдельно скачаны **7 бандлов webapp-плагинов** по `/static/plugins/<id>/<bundle>.js`
   (путь из манифеста `/api/v4/plugins/webapp` даёт 404 — в нём нет сегмента `plugins`).

2. **Извлечение маршрутов.** Скрипт `extract_routes2.py` (переписан относительно прошлого захода):
   собирает определения `get*Route(){return \`…\`}`, рекурсивно разворачивает их внутри
   шаблонных строк, подставляет `{}` вместо `${…}` с учётом **вложенных скобок**
   (старая версия ломалась на `${buildQueryString({page:e})}` и оставляла хвосты вида `{})}`),
   отрезает query-string и определяет HTTP-метод по ближайшему `method:"…"` в окне 500 символов —
   это ловит и `doFetch(\`tpl\`,{method})`, и `const s=\`tpl\`; … doFetch(s,{method})`,
   на котором прошлый заход терял все опросы.
   Отдельный проход `client4_methods.py` привязывает имена методов `Client4`
   (`getPinnedChannels`, `forwardPosts`, …) к парам «метод · путь» — из имён читается назначение.

3. **Двойная ванильная база.** Вычиталось не только из вендоренного OpenAPI-спека
   (`spec/mattermost-openapi-v4.yaml`, **468 путей / 585 операций**), который заведомо неполон,
   но и из **апстримного `Client4`, вшитого в бандлы плагинов**. Плагины `com.mattermost.calls`,
   `mattermost-ai` и `ru.magnit.…ews-calendar` тянут npm-пакет `@mattermost/client` целиком —
   это класс, метод-левел tree-shaking к нему не применяется, поэтому в бандле лежит
   **весь апстримный набор маршрутов**: 381 путь. Это независимый эталон «как выглядит ванильный клиент».
   Кастом = путь есть в чанках форка, но его нет ни в спеке, ни в апстримном клиенте.

### Числа

| Показатель | Значение |
|---|---|
| Чанков webapp разобрано | **261** (259 из карты, 258 скачано + 3 точки входа), 22 МБ |
| Бандлов плагинов разобрано | **7** |
| Определений `get*Route` в форке | **82** |
| Методов `Client4` с однозначным HTTP-глаголом | **410** |
| Пар «метод · путь», извлечено из webapp | **607** |
| Уникальных путей `/api/v4` из webapp | **442** |
| Ванильная база: спек | 468 путей / 585 операций |
| Ванильная база: апстримный `Client4` из бандлов | 381 путь |
| **Кастомных путей после двойного вычитания** | **43** |
| **Кастомных операций (метод + путь)** | **55** |
| Плагинных путей (`/plugins/*`) | 9 из бандлов + 7 подтверждено вручную |

### Что двойная база отменила из прошлого отчёта

Одиночное вычитание из спека давало 130 «кастомных» путей — из них 87 оказались
**апстримом, которого просто нет в вендоренном спеке** (`/cloud`, `/usage`, `/reports`,
`/content_flagging`, `/limits`, `/sharedchannels`, `/permissions`, `/system/notices`,
`/recaps`, `/agents`, `/llmservices`, кросс-командный `POST /api/v4/posts/search`
и т.д.). Это прямо влияет на выводы: **автослой не покрывает не только кастом ТЭГ,
но и заметный кусок штатного API 11.9** — спек отстаёт от сервера.

Отдельно: **burn-on-read оказался апстримной фичей**, а не кастомом ТЭГ.
`GET /api/v4/posts/{id}/reveal` и `DELETE /api/v4/posts/{id}/burn` есть и в спеке,
и в апстримном клиенте. На стенде фича выключена (`EnableBurnOnRead: false`),
но пост типа `burn_on_read` в town-square лежит: `props = {expire_at, read_duration}`.

И поправка к `tag-test-recon.md`: **`PUT /api/v4/polls/{post_id}` не существует**.
В `Client4` форка ровно четыре поллных метода — `vote`, `revoke`, `close`, `details`.
Создание и редактирование опроса идут через обычные `POST`/`PUT /api/v4/posts`
с `type: "poll"` и `props.poll_config` (видно в `useCallback` редактора опроса, чанк 8860).

---

## 2. Таблица кастомных эндпоинтов

Столбец «Доступ» — фактический код ответа моим токеном (`system_user`) на стенде.
«Форма ответа» — структура полей, значения не приводятся.

### 2.1 Голосования (`EnablePolls: true`, `MaxPollOptions: 15`)

| Метод · путь | `Client4` | Назначение | Доступ | Форма ответа |
|---|---|---|---|---|
| `GET /api/v4/polls/{post_id}/details` | `getPollsPostIdDetails` | Полные результаты, включая поимённые голоса | **400** `app.poll.invalid_type` на не-опросном посте; **403** `app.poll.no_permission` на чужом опросе | `{config:{question,options[],allow_multiple,anonymous,disallow_vote_change}, option_counts[], total_votes, votes[{choices[]}], last_vote_at, ended_at, current_user_voted, current_user_choice}` |
| `POST /api/v4/polls/{post_id}/vote` | `postPollsPostIdVote` | Проголосовать, тело `{choices:[…]}` | пишущий, не вызывался | — |
| `POST /api/v4/polls/{post_id}/revoke` | `postPollsPostIdRevoke` | Отозвать свой голос | пишущий | — |
| `POST /api/v4/polls/{post_id}/close` | `postPollsPostIdClose` | Закрыть голосование | пишущий | — |

**Чтение опросов отдельного API не требует.** Всё лежит в самом посте:
`type: "poll"`, `props.poll_config`, `metadata.poll` — и приходит обычными
`GET /posts/{id}`, `GET /channels/{id}/posts`, `GET /posts/{id}/thread`.

### 2.2 Достижения (`EnableAchievements: true`)

| Метод · путь | `Client4` | Назначение | Доступ | Форма ответа |
|---|---|---|---|---|
| `GET /api/v4/achievements?page&per_page` | `getAchievements` | Каталог достижений | **200** (14 шт.) | `[{id, display_name, description, type, system, user_id, create_at, update_at, delete_at}]` |
| `GET /api/v4/achievements/admin?page&per_page&include_total` | `getManageableAchievements` | Достижения, которыми я могу управлять | **200**, пустой массив | как выше |
| `GET /api/v4/achievements/{id}` | `getAchievement` | Карточка достижения | **404** `app.achievement.get.not_found` — обычному не отдаётся | — |
| `GET /api/v4/achievements/{id}/icon?t=` и `/icon/preview?t=` | `getAchievementIconUrl` | Иконка | **200** `image/png` (34 КБ) | бинарь |
| `GET /api/v4/achievements/grant/{user_id}` | `getUserAchievements` | Что выдано пользователю | **200**, пусто | массив грантов |
| `GET /api/v4/achievements/favorites/user/{user_id}` | `getFavoriteAchievements` | Избранные пользователя | **200**, пусто | массив |
| `POST /api/v4/achievements` (multipart `achievement`+`icon`) | `createAchievement` | Создать | пишущий | — |
| `PUT /api/v4/achievements/{id}` | `updateAchievement` | Изменить | пишущий | — |
| `DELETE /api/v4/achievements/{id}` | `deleteAchievement` | Удалить | пишущий | — |
| `POST /api/v4/achievements/grant` | `grantAchievement` | Выдать | пишущий | — |
| `DELETE /api/v4/achievements/grant/{a}/{u}` | `revokeAchievementGrant` | Отозвать | пишущий | — |
| `POST` / `DELETE /api/v4/achievements/favorites/{id}` | `add/removeFavoriteAchievement` | Избранное | пишущий | — |
| `PUT /api/v4/achievements/favorites` | `replaceFavoriteAchievements` | Переупорядочить, `{achievement_ids:[…]}` | пишущий | — |

### 2.3 Кросс-командный список каналов — главная находка

| Метод · путь | `Client4` | Назначение | Доступ |
|---|---|---|---|
| `GET /api/v4/users/{user_id}/channels/all` | `getAllChannelsForUser` | Все каналы пользователя **сразу по всем командам** | **200** |
| то же `?include_preview=true&page&per_page&team_ids&term&sort_by` | `getAllChannelsWithPreview` | То же + превью последнего сообщения, непрочитанные, mute, закрепление; `term` — поиск по названию | **200** (`sort_by=last_post_at` валиден, прочие значения → 400) |

Форма ответа (`/users/me/channels/all?include_preview=true`):

```
{ total_count,
  channels: [ {
    channel: { …штатные поля Channel…,
               team_name, team_display_name, team_update_at,      ← кастом: команда прямо в канале
               e2ee, autotranslation, discoverable,               ← кастомные флаги форка
               default_category_name, managed_category_name,
               last_channel_icon_update, policy_id,
               policy_enforced, policy_is_active },
    preview_post: { id, create_at, update_at, message, user_id,
                    has_files, has_images, is_reply, is_forward },
    unread: { root_unread_count, root_mention_count,
              thread_unread_count, thread_mention_count },
    notification_state: { mode, is_muted,
                          should_highlight_unread, should_highlight_mention },
    is_pinned } ] }
```

Один вызов заменяет для ассистента цепочку «команды → каналы команды → членства →
непрочитанные → последний пост канала». Работает с `me`, поддерживает поиск и пагинацию.

### 2.4 Кросс-командные треды и закреплённые каналы

| Метод · путь | `Client4` | Назначение | Доступ | Форма ответа |
|---|---|---|---|---|
| `GET /api/v4/users/{id}/threads?page&per_page&team_id&term` | `getUnifiedThreads` | Треды сразу по всем командам (в апстриме только `/teams/{team_id}/threads`), с поиском по `term` | **200** | `{total, total_unread_threads, total_unread_mentions, total_unread_urgent_mentions, threads[]}` |
| `GET /api/v4/users/{id}/threads/followed` | `getFollowedThreadsForUser` | Треды, на которые подписан | **200** | `{threads, total_count}` |
| `GET /api/v4/users/{id}/channels/pinned` | `getPinnedChannels` | Закреплённые каналы (пины сайдбара, не пины сообщений) | **200** | `{channel_ids: []}` |
| `PUT /api/v4/users/{id}/channels/pinned` | `updatePinnedChannels` | Задать список, `{channel_ids:[…]}` | пишущий | — |

### 2.5 Пересылка и отметки прочтения

| Метод · путь | `Client4` | Назначение | Доступ |
|---|---|---|---|
| `POST /api/v4/posts/forward` | `forwardPosts` | Массовая пересылка выделенных постов (`EnableForwards: true`, `MaxPostsPerForward: 10`, `ForwardPreserveInReplyTo: true`) | пишущий |
| `PUT /api/v4/read_statuses/{post_id}` | `setReadStatuses` | Проставить отметку «прочитано» по конкретному сообщению | пишущий |

**Чтение отметок отдельного эндпоинта не имеет** — приходит в `post.metadata.read_statuses`
(редьюсер `RECEIVED_POST_READ_STATUSES` в чанке 4285, UI «Message was read / N users» в 9338).
`ReadStatusesEnable: true`, `ReadStatusesChannelMembersLimit: 50`, `ReadStatusesEnabledChannelIDs` пуст.
Как и с опросами — данные уже едут в обычном ответе, вопрос только в том, доносим ли мы их до модели.

### 2.6 E2EE (`EnableE2EE: true`, лимит 10 участников, файл до 50 МБ)

| Метод · путь | `Client4` | Доступ | Форма |
|---|---|---|---|
| `GET /api/v4/e2ee/pubkey` | `getE2EEPublicKey` | **200**, `null` (ключа нет) | ключ или `null` |
| `PUT` / `DELETE /api/v4/e2ee/pubkey` | `save/deleteE2EEPublicKey` | пишущий | — |
| `POST /api/v4/e2ee/pubkey/users` | `getE2EEPublicKeysForUsers` | пишущий по методу, читающий по смыслу | ключи списка пользователей |
| `GET /api/v4/e2ee/backup` | `getE2EEKeysBackup` | **404** `no_pub_keys` | — |
| `POST /api/v4/e2ee/backup` | `saveE2EEKeysBackup` | пишущий | — |

Есть и поле `channel.e2ee` в карточке канала. Содержимое E2EE-каналов ассистенту недоступно
по построению — ключи у клиента.

### 2.7 Настройки и доступ к каналу

| Метод · путь | `Client4` | Назначение | Доступ | Форма |
|---|---|---|---|---|
| `GET /api/v4/channels/{id}/preferences` | `getChannelPreferences` | Кастомные настройки канала | **200** | `[{channel_id, name, value}]`; на стенде ключи `disable_join_leave_messages`, `disable_access_requests` |
| `GET /api/v4/teams/name/{team}/channels/name/{ch}/preferences/{key}` | `getChannelPreferenceByTeamAndName` | Одна настройка по человекочитаемым именам | **404**, если записи нет | одна пара |
| `POST /api/v4/channels/{id}/access_request` | `requestChannelAccessById` | Запросить доступ в закрытый канал | пишущий | — |
| `POST /api/v4/teams/name/{t}/channels/name/{c}/access_request` | `requestChannelAccess` | То же по именам | пишущий | — |
| `POST` / `DELETE /api/v4/channels/{id}/image` | `setChannelIcon` / `removeChannelIcon` | Иконка канала (в апстриме таких нет) | пишущий | — |

### 2.8 Профиль и приглашения

| Метод · путь | `Client4` | Назначение | Доступ |
|---|---|---|---|
| `GET /api/v4/users/{id}/image/full` | `getFullProfilePictureUrl` | Фото профиля в полном размере (апстрим отдаёт только превью `/image`) | **200** `image/png` |
| `POST /api/v4/teams/{id}/invite-guests/instant` | `createGuestInstant`, `addExistingGuestInstant` | Мгновенное гостевое приглашение без письма | пишущий |

### 2.9 Недоступно моим токеном — фиксирую как «недоступно» (403)

| Метод · путь | Назначение | Код |
|---|---|---|
| `GET /api/v4/teams/{id}/policy_bypass` | Список исключений из политик ABAC на уровне команды | **403** |
| `GET /api/v4/teams/{id}/members/{uid}/policy_bypass` | Исключение по конкретному участнику | **403** |
| `GET /api/v4/ldap/check_user?username=` | Есть ли пользователь в LDAP | **403** |
| `GET /api/v4/ldap/sync/conflicts?page&per_page` | Конфликты синхронизации LDAP | **403** |
| `GET /api/v4/ldap/users/{id}/diagnostics` | Диагностика привязки учётки к LDAP | **403** |
| `GET /api/v4/ldap/users/{id}/groups/{gid}/joined-at` | Когда пользователь попал в LDAP-группу | **403** |
| `POST` / `DELETE /api/v4/ldap/groups/link?remote_id=` | Привязать/отвязать LDAP-группу | админский |
| `GET` / `PUT /api/v4/access_control_policies/admin_priorities` | Приоритеты ABAC-политик | **403** |
| `GET` / `POST /api/v4/access_control_policies/provision_rules` | Правила автопровижининга по атрибутам | **403** |
| `DELETE /api/v4/access_control_policies/provision_rules/{id}` и `/orphaned` | Удаление правил | админский |

### 2.10 Служебное

| Метод · путь | Назначение |
|---|---|
| `POST /api/v4/client_stall` (`reportClientStall`) | Телеметрия зависаний клиента. Для ассистента бесполезно |
| `POST /api/v4/users/{id}/cache/invalidate` (`invalidateUserCache`) | Сброс кэша пользователя. Админское |

### 2.11 Плагинные эндпоинты (перепроверено)

| Плагин | Путь | Метод | Доступ | Форма |
|---|---|---|---|---|
| `ru.magnit.userinfo` | `/plugins/ru.magnit.userinfo/api/v1/user/{id}/custom_profile` | GET | **200** | `{structureHierarchy:[{order, displayName, value}], structureText, isDepartmentOnly, managerUser:{…полный User…}, managerDisplayName, telephone}` |
| `ru.magnit.…ews-calendar` | `/api/v1/client_config` | GET | **200** | `{allowedShowCalendar, allowedTeamIds}` |
| то же | `/api/v1/event_list?startdate=&enddate=&userTimeZone=` | GET | **500** на стенде (нет связки с EWS) | список событий |
| то же | `/api/v1/event/{id}`, `/api/v1/event/{id}/meeting_accept/` | GET / POST | не проверялись | — |
| `ru.magnit.bulk-invite` | `/handlers/channel_bulk_add/status?channel_id=` | GET | **400** (нужен активный job) | статус фоновой задачи |
| то же | `/handlers/channel_bulk_add`, `/cancel`, `/import_from_channel` | POST | пишущие | — |
| `com.opscenter.cards` | своего API нет | — | — | рисует пост-тип `custom_opscenter_card`, действия — штатные `POST /posts/{id}/actions/{action_id}` |

`custom_profile` — единственный по-настоящему ценный читающий плагинный эндпоинт:
оргструктура и руководитель, чего нет в штатном профиле MM.

---

## 3. Фичи по смыслу: ценность для ИИ-ассистента и сложность

Ценность — насколько это меняет качество ответов ассистента. Сложность — S (правка модели
или один GET), M (новый инструмент с резолвом и пагинацией), L (несколько эндпоинтов, состояние, запись).

| Фича | Ценность | Слож. | Почему |
|---|---|---|---|
| **`props`/`metadata` в постах** (опросы, отметки прочтения, карточки OpsCenter, приоритет, ack) | **высокая** | **S** | Данные уже приходят в каждом ответе `get_post`/`get_channel_posts`/`get_thread`, а `PostSummary` их выбрасывает. Пост опроса доезжает как `type="poll"` без вопроса, вариантов и результатов. Правка одной модели снимает сразу четыре фичи |
| **Кросс-командный список каналов** `users/me/channels/all?include_preview` | **высокая** | **M** | Один вызов вместо цепочки из 4–5. Отдаёт непрочитанные, mute, превью последнего сообщения, команду, закрепление. Прямо закрывает «что у меня происходит» — самый частый вопрос к ассистенту |
| **Кросс-командные треды** `users/me/threads` (+`/followed`) | **высокая** | **M** | Треды у нас не покрыты **вообще** — ни курируемым слоем, ни автослоем (в спеке только team-scoped вариант). А CRT на стенде включён, и значительная часть переписки живёт в тредах |
| **Оргструктура** `userinfo/custom_profile` | **высокая** | **S** | Дирекция, департамент, руководитель, телефон. Небольшой стабильный ответ. Позволяет отвечать «кто чей руководитель», «из какого подразделения» — то, чего в MM нет в принципе |
| **Голосования: чтение** | **высокая** | **S** | Фича №1 у тимлида. Закрывается пробросом `props`/`metadata`, отдельный эндпоинт не нужен |
| **Голосования: детали** `polls/{id}/details` | средняя | S | Даёт поимённые голоса, но только по своим опросам (на чужих 403). Ограниченная применимость |
| **Голосования: голосовать/закрывать** | средняя | M | Пишущие. Полезно для «проголосуй за меня», но требует `MM_WRITE_MODE=full` и подтверждения |
| **Отметки прочтения: чтение** | средняя | S | «Кто прочитал объявление» — понятный сценарий для дежурных и руководителей. Идёт вместе с пробросом `metadata` |
| **Пересылка постов** `posts/forward` | средняя | M | Массовая (до 10). Хороший сценарий «перекинь эту ветку в канал X», но пишущий и с риском рассылки не туда |
| **Достижения: чтение** | низкая | S | Каталог доступен, но гранты и избранное на стенде пусты, карточка достижения обычному не отдаётся (404). Для рабочих задач ассистента не нужны |
| **Достижения: выдача** | низкая | M | Чистая геймификация, пишущая |
| **Настройки канала** `channels/{id}/preferences` | низкая | S | Два флага про join/leave-сообщения и запросы доступа. Диагностика, не рабочий сценарий |
| **Закреплённые каналы** `users/me/channels/pinned` | низкая | S | Уже приходит как `is_pinned` внутри `channels/all` — отдельный тул избыточен |
| **Запрос доступа в канал** | низкая | S | Пишущий, разовый, человек делает сам за два клика |
| **Иконка канала, фото в полный рост** | низкая | S | Бинарь. У нас уже есть `download_file`, отдельные тулы не окупаются |
| **Гостевые приглашения instant** | низкая | M | Пишущий, административный |
| **E2EE** | низкая | L | Ассистент не может и не должен расшифровывать. Максимум — «канал зашифрован, содержимое недоступно», а это поле `channel.e2ee` в обычной карточке |
| **LDAP-диагностика, ABAC provision_rules, policy_bypass** | низкая | — | **403** обычным пользователем. Недоступно, обсуждать нечего до появления сервисной учётки |
| **`client_stall`, `cache/invalidate`** | нулевая | — | Телеметрия и админщина |

**Отдельно, не кастом, но обнаружено попутно и важно:** кросс-командный поиск
`POST /api/v4/posts/search` **без `team_id`** работает (200, `EnableCrossTeamSearch: true`),
а наш `search_posts` жёстко ходит в `/teams/{team_id}/posts/search`
(`tag_mcp/tools/search.py:92`). Ценность **высокая**, сложность **S** —
опциональный параметр, а не новый инструмент.

---

## 4. Слэш-команды

`GET /api/v4/users/me/teams` → одна команда `big-tasty`; `GET /api/v4/commands?team_id=…` → **37 команд**.
`GET /api/v4/teams/{id}/commands/autocomplete` → те же 37 с деревом подсказок.
У всех пустые `plugin_id` и `url` — это встроенные и плагинные команды, зарегистрированные
сервером, а не вебхук-интеграции.

**Нештатные (плагинные) — 8:**

| Команда | Подкоманды | Что закрывает | Нужен ли новый код |
|---|---|---|---|
| `/business_metrics` | `add_downtime`, `list_downtime`, `help` | Учёт простоев сервисов. Самое интересное для дежурств и разборов инцидентов | **нет** |
| `/autolink` | `add`, `delete`, `disable`, `enable`, `list`, `set`, `test` | Автолинковка по regex (номера задач → ссылки) | **нет** |
| `/welcomebot` | `set/get/delete_channel_welcome`, `set/get/delete_team_welcome` | Приветственные сообщения канала и команды | **нет** |
| `/mobile-logs` | `[on\|off\|status] [@username]` | Сбор логов мобильного клиента | **нет** |
| `/call` | `start`, `join`, `leave`, `link`, `end`, `stats`, `recording`, `logs`, `host` | Звонки | **нет** |
| `/secure-connection` | `create`, `accept`, `remove`, `status` | Межинстансные соединения | **нет** |
| `/share-channel` | `invite`, `uninvite`, `unshare`, `status` | Общие каналы | **нет** |
| `/hello` | `[@username]` | Демо-плагин | **нет** |

Остальные 29 — штатные Mattermost: `/msg`, `/join`, `/open`, `/invite`, `/kick`, `/remove`,
`/leave`, `/header`, `/purpose`, `/rename`, `/search`, `/dnd`, `/away`, `/online`, `/offline`,
`/status`, `/settings`, `/shortcuts`, `/help`, `/logout`, `/me`, `/echo`, `/shrug`, `/code`,
`/collapse`, `/expand`, `/groupmsg`, `/mute`, `/marketplace`.

**Вывод:** **все 37 закрываются без единой строки нового кода.** `list_slash_commands`
уже показывает их вместе с деревом автодополнения, `execute_slash_command` уже умеет их
выполнять. Ограничение чисто конфигурационное: `execute_slash_command` помечен `write`
и в `MM_WRITE_MODE=readonly` скрыт. Голосования слэш-командой не создаются — `/poll`
на стенде нет, опросы вшиты в ядро (отличие от типовых инсталляций с `matterpoll`).

---

## 5. Покрытие нашим MCP

Проверено на `magnit-tag-mcp`, ветка `feature/oauth-mode`:
`uv run python -m tag_mcp --list-tools` → **66 тулов, 40 видно, 26 скрыто политикой**
(20 `write`, 3 `destructive`, 1 `admin`, 2 по обоим). `CURATED_CALLS` в
`tests/contract/test_spec_contract.py` — **65 путей**, все из ванильного спека.

| Кастомная возможность | Статус | Комментарий |
|---|---|---|
| Опросы — чтение | **нужна правка** | Эндпоинты уже дёргаются (`get_post`, `get_channel_posts`, `get_thread`), но `PostSummary` (`tag_mcp/models.py`) отбрасывает `props` и `metadata` кроме `files`/`reactions` |
| Опросы — детали / голосование / закрытие | **нужен инструмент** | 4 кастомных эндпоинта, ни один не покрыт |
| Отметки прочтения — чтение | **нужна правка** | Та же правка `PostSummary`: `metadata.read_statuses` |
| Отметки прочтения — простановка | **нужен инструмент** | `PUT /read_statuses/{id}` |
| Пересылка постов | **нужен инструмент** | `POST /posts/forward` |
| Кросс-командный список каналов | **нужен инструмент** | `list_my_channels` ходит в team-scoped `/users/{id}/teams/{team_id}/channels` |
| Кросс-командные треды | **нужен инструмент** | Треды не покрыты ни курируемым слоем, ни автослоем — в спеке только team-scoped |
| Кросс-командный поиск постов | **нужна правка** | `search_posts` жёстко team-scoped, `tag_mcp/tools/search.py:92` |
| Оргструктура (`userinfo`) | **нужен инструмент** | Плагинный путь, автослой его не видит |
| Достижения (каталог, гранты, избранное) | **нужен инструмент** (низкий приоритет) | Читающие доступны, но ценность низкая |
| Настройки канала, закреплённые каналы, иконки, фото в полный рост | **нужен инструмент** (низкий приоритет) | Или закрываются полями внутри `channels/all` |
| E2EE | **осознанно не покрываем** | Ассистент не расшифровывает; факт шифрования виден по `channel.e2ee` |
| LDAP-диагностика, ABAC provision_rules, policy_bypass | **недоступно** | 403 обычным пользователем |
| `client_stall`, `cache/invalidate`, гостевые приглашения, запрос доступа | **не нужно** | Телеметрия и админщина |
| Слэш-команды (все 37) | **покрыто** | `list_slash_commands` + `execute_slash_command`; вопрос только в `MM_WRITE_MODE` |
| Календарь EWS | **отложено** | `event_list` на стенде отдаёт 500 |
| Bulk-invite, OpsCenter Cards, Calls, ONLYOFFICE | **не нужно** | Пишущие / UI-плагины |

**Системный вывод.** Автослой строится из вендоренного OpenAPI-спека, поэтому
кастомные пути он не покрывает **в принципе** — и, как выяснилось выше, не покрывает
ещё и ~87 путей штатного 11.9, которых в спеке нет. Грепом по `tag_mcp/`:
ни одного упоминания polls / achievements / forwards / read_statuses / e2ee / custom_profile.

---

## 6. Рекомендованный состав доработки

15 инструментов. Теги — по нашей действующей схеме (`read` / `write` / `destructive` / `lite`).

### Приоритет 1 — правки без новых тулов (делать первыми)

| № | Что | Где | Обоснование |
|---|---|---|---|
| 0.1 | Пробросить `props` и `metadata` в `PostSummary`, разобрать `poll_config` / `metadata.poll` / `metadata.read_statuses` в человекочитаемый вид | `tag_mcp/models.py` | Одна правка закрывает опросы, отметки прочтения, карточки OpsCenter и агентские посты. Данные уже приходят — мы их выбрасываем |
| 0.2 | `search_posts`: параметр `all_teams` → ходить в `/api/v4/posts/search` без `team_id` | `tag_mcp/tools/search.py:92` | `EnableCrossTeamSearch: true`, проверено — 200. Сейчас ассистент не видит половину переписки |
| 0.3 | Починить `has_more` в `list_my_channels` | `tag_mcp/tools/channels.py:54`, `tag_mcp/models.py:307` | Условие `len >= per_page > 0` истинно всегда — модель уходит за несуществующей страницей |

### Приоритет 2 — новые инструменты, высокая ценность

| № | Имя | Назначение | Эндпоинт | Теги | Обоснование |
|---|---|---|---|---|---|
| 1 | `list_all_my_channels` | Все каналы по всем командам с непрочитанными, mute, превью последнего сообщения и командой | `GET /users/me/channels/all?include_preview=true&page&per_page&term&sort_by=last_post_at` | `read`, `lite` | Один вызов вместо 4–5. Прямо закрывает «что у меня происходит» |
| 2 | `list_my_threads` | Треды по всем командам: непрочитанные, упоминания, срочные | `GET /users/me/threads?page&per_page&team_id&term` | `read`, `lite` | Треды не покрыты вообще; CRT включён |
| 3 | `get_thread_participation` | Треды, на которые я подписан | `GET /users/me/threads/followed` | `read` | Дешёвый спутник №2 |
| 4 | `get_org_profile` | Оргструктура сотрудника: подразделение, дирекция, руководитель, телефон | `GET /plugins/ru.magnit.userinfo/api/v1/user/{id}/custom_profile` | `read` | Единственный источник оргданных; в MM их нет |
| 5 | `get_poll` | Полные результаты опроса: вопрос, варианты, счётчики, мой голос | `GET /posts/{id}` + `GET /polls/{id}/details` с деградацией на 403 | `read`, `lite` | Фича №1 у тимлида. Собирается из поста, детали — бонусом когда доступны |
| 6 | `get_post_read_receipts` | Кто прочитал сообщение | `GET /posts/{id}` → `metadata.read_statuses` + резолв имён | `read` | «Все ли увидели объявление» — понятный сценарий для дежурных |

### Приоритет 3 — пишущие, за `MM_WRITE_MODE=full`

| № | Имя | Назначение | Эндпоинт | Теги | Обоснование |
|---|---|---|---|---|---|
| 7 | `vote_in_poll` | Проголосовать | `POST /polls/{id}/vote` `{choices}` | `write` | Естественное продолжение №5 |
| 8 | `revoke_poll_vote` | Отозвать голос | `POST /polls/{id}/revoke` | `write` | Обратимая пара к №7 — обязательна |
| 9 | `close_poll` | Закрыть голосование | `POST /polls/{id}/close` | `destructive` | Необратимо для участников |
| 10 | `forward_posts` | Переслать до 10 сообщений в канал | `POST /posts/forward` | `write` | «Перекинь эту ветку в канал X». Риск рассылки не туда — нужен явный `channel_id` и подтверждение |
| 11 | `mark_post_read` | Отметка «прочитано» по сообщению | `PUT /read_statuses/{id}` | `write` | Пара к №6 |

### Приоритет 4 — низкая ценность, делать если останется время

| № | Имя | Назначение | Эндпоинт | Теги | Обоснование |
|---|---|---|---|---|---|
| 12 | `list_achievements` | Каталог достижений | `GET /achievements?page&per_page` | `read` | Дёшево, но рабочих сценариев нет |
| 13 | `get_user_achievements` | Достижения и избранное пользователя | `GET /achievements/grant/{id}` + `/favorites/user/{id}` | `read` | На стенде пусто |
| 14 | `get_channel_settings` | Кастомные настройки канала | `GET /channels/{id}/preferences` | `read` | Диагностика «почему не видно join/leave» |
| 15 | `pin_channel` / `unpin_channel` | Закрепить канал в сайдбаре | `GET`+`PUT /users/me/channels/pinned` | `write` | Чтение уже есть в №1 как `is_pinned`; только запись |

**Не рекомендуется делать:** E2EE (ассистент не расшифровывает), LDAP-диагностика и
ABAC provision_rules (403), `client_stall` и `cache/invalidate` (телеметрия/админщина),
иконки каналов и фото в полный рост (закрывается `download_file`), запрос доступа
в канал и гостевые приглашения (разовые действия, человек делает сам),
слэш-команды (уже покрыты).

---

## 7. Оверлей OpenAPI-спека для кастомных путей — вердикт

**Рекомендация: делать, но маленький и только для описанных выше кастомных путей —
не как замену курируемым тулам, а как страховку и документацию.**

Механика простая. `load_spec()` (`tag_mcp/openapi_layer.py:245`) читает **один** YAML,
дальше `sanitize_spec` и `spec_operations`. Оверлей — второй файл
`spec/tag-custom-overlay.yaml` с 55 операциями, который сливается в `paths`
после разбора базового. Правка на ~20 строк, схема слияния — по ключу `(path, method)`.

**За:**

- **Автослой начинает видеть кастом.** При `MM_ENABLE_GENERATED=true` появляются
  `api_GetAllChannelsForUser`, `api_GetUnifiedThreads` и прочие — «длинный хвост»
  из 55 операций закрывается без 55 курируемых тулов.
- **Контрактный тест перестаёт врать.** Сейчас `test_curated_call_exists_in_the_spec`
  физически не даст добавить в `CURATED_CALLS` кастомный путь: его нет в спеке.
  Любой из 15 тулов выше упрётся в этот тест. Оверлей снимает блокировку **до** написания тулов.
- **Спек становится документацией форка.** Единственное место, где записано, чем ТЭГ
  отличается от апстрима, — иначе это знание живёт в этом отчёте и умрёт вместе с ним.
- **Политики применяются автоматически.** `ADMIN_TAGS`, `DESTRUCTIVE_OPERATION_RE`,
  `FORBIDDEN_OPERATION_RE` работают по тегам и `operationId`, то есть если проставить
  кастомным операциям теги `ldap`/`access_control` и осмысленные `operationId`,
  LDAP-диагностика и ABAC уедут в admin-домен сами, без отдельного кода.

**Против:**

- **Оверлей нужно вести руками.** Форк живёт своей жизнью; при обновлении стенда
  расхождение обнаружится только повторным разбором чанков. Смягчается тем, что
  скрипты сбора (`fetch`, `extract_routes2.py`, `client4_methods.py`) уже написаны
  и прогоняются за пару минут — можно завести регулярную сверку.
- **Схемы ответов придётся описывать самому.** У нас нет исходников форка, только
  формы, снятые с живого стенда. Полноценных `components/schemas` не будет —
  реалистично описать `paths`, параметры и коды ответов, а тела оставить
  свободными объектами. Автосгенерированные тулы получатся с бедными описаниями.
- **Автослой и так не рекомендуется как основной путь.** В докстринге `openapi_layer.py`
  прямо сказано: с автоконвертированными серверами модели работают заметно хуже.
  Пять высокоценных фич (кросс-командные каналы и треды, опросы, оргструктура,
  отметки прочтения) всё равно надо делать курируемыми тулами — оверлей их не заменяет.
- **Есть риск ложной уверенности.** Операция в спеке ≠ операция доступна: половина
  кастома отдаёт 403 обычному пользователю. Нужно проставлять `403` в `responses`
  и/или помечать эти пути admin-тегами, иначе модель будет ходить в стену.

**Оценка:** механика слияния — **S** (~20 строк в `load_spec` + тест). Написание самого
оверлея на 55 операций с параметрами и кодами — **M** (день работы, материал целиком
в разделе 2 этого отчёта). Итого **S+M**, окупается в основном тем, что снимает
блокировку контрактного теста для 15 тулов и фиксирует знание о форке в репозитории.

**Порядок работ:** сначала правки 0.1–0.3 (дешевле всего, максимальный эффект),
затем оверлей (разблокирует контрактный тест), затем курируемые тулы 1–6,
затем пишущие 7–11, и только потом хвост 12–15.

---

## 8. Оговорки

- Всё, что помечено «пишущий», **не вызывалось**. Методы и формы тел взяты из кода
  `Client4`, а не из ответов сервера.
- Ванильная база — спек из нашего репозитория (468 путей) плюс апстримный `Client4`
  из бандлов плагинов (381 путь). Второй источник может отставать от 11.9.4 на минорную
  версию, поэтому единичные «кастомные» пути теоретически могут оказаться свежим апстримом.
  Наиболее вероятные кандидаты на такую ошибку — `channels/{id}/access_request`
  и `teams/{id}/policy_bypass`; ядро находок (polls, achievements, e2ee, forward,
  read_statuses, channels/all, unified threads) сомнений не вызывает.
- Разбор ведётся по webapp. Серверные эндпоинты, которых нет в вебе (например, только для
  мобильных клиентов), этим методом не видны.
- Проверено с правами `system_user`. С сервисной учёткой картина по 403-эндпоинтам изменится.
