# Отчёт: I-7 приоритет 3 — пишущие инструменты кастомного API ТЭГ

Репозиторий `magnit-tag-mcp`, ветка `feature/oauth-mode`, коммиты локальные (не пушились).
Основание: `reports/tag-custom-api-map.md` (раздел 6, приоритет 3; разделы 2.1 и 2.5)
и `reports/tz-i7-tag-custom-api.md` (блок B).

## Реализовано

Пять пишущих инструментов, все — в `tag_mcp/tools/posts.py`, все за режимом записи
(`MM_WRITE_MODE`), через `guard_write()` и с возвратом `WriteResult`:

| Тул | Эндпоинт форка | Теги | Подтверждение |
|---|---|---|---|
| `vote_in_poll` | `POST /polls/{post_id}/vote` `{choices}` | `write` | как у `create_post` |
| `revoke_poll_vote` | `POST /polls/{post_id}/revoke` | `write` | как у `create_post` |
| `close_poll` | `POST /polls/{post_id}/close` | `destructive` | плюс `MM_ALLOW_DESTRUCTIVE=true` |
| `forward_posts` | `POST /posts/forward` | `write`, `sensitive` | спрашивает даже при `MM_CONFIRM_DM=false` |
| `mark_post_read` | `PUT /read_statuses/{post_id}` | `write`, `WriteScope.SELF` | не спрашивает (как `mark_channel_read`) |

Механизм видимости — тот же, что у существующих пишущих: тег политики + `tool_visible()`.
В `readonly` тулы не видны вовсе, в `confirm` спрашивают человека, в `full` пишут сразу
(стоп-кран `MM_WRITE_BURST_LIMIT` действует и там). `close_poll` дополнительно скрыт без
`MM_ALLOW_DESTRUCTIVE=true`.

Что решается **до** записи: не-опрос, закрытый опрос, вариант, которого в опросе нет
(отказ называет настоящие варианты), второй вариант в опросе без `allow_multiple`,
отсутствующий голос при отзыве, лимит `MaxPostsPerForward` = 10, нечитаемое сообщение
в пачке пересылки (пересылается либо всё, либо ничего). Тексты вариантов переводятся
в номера (`current_user_choice: [0]` в деталях опроса — свидетельство формы).
`idempotent` нигде не ставится: повтор этих POST форк не дедуплицирует.

Оверлей OpenAPI-спека **не делался**: в карте он описан отдельным разделом 7, а не как
часть приоритета 3. Блокировку контрактного теста, ради которой он был нужен, приоритет 2
снял списком `CUSTOM_CALLS` — пять новых путей внесены туда же.

## Деградация на ванильном стенде

Все пять путей — код форка, у Mattermost 11.9 их нет. `fork_write_failed()` переводит
404 и 501 в понятный `not_found`: «возможность на этом сервере недоступна», в подсказке —
что проверить `MM_URL`. Отказы остальных видов (403, 400) не подменяются: сервер
объясняет их сам. Сообщение к этому моменту уже прочитано обычным `GET /posts/{id}`,
поэтому 404 на пишущем пути однозначно означает сервер, а не сообщение.

## Тесты

- **Юниты:** `tests/unit/test_custom_tools.py` — 5 новых классов, 34 теста
  (89 в файле). Полный прогон: **1052 passed, 69 skipped**.
- **Покрытие:** `tag_mcp/tools/posts.py` — **100 %** (422 stmt, 0 miss). По проекту 99 %:
  единственная непокрытая строка — `tag_mcp/supervisor.py:92` (`raise SystemExit(main())`),
  её покрывает только `tests/resilience/test_supervisor.py::TestLoggingAndEntry::
  test_module_is_executable`, который приходится исключать (виснет). Прогон без
  `--deselect` в CI даёт 100 %.
- **Страховки:** новые тулы внесены в `CONFIRMED_TOOLS`/`QUIET_TOOLS`
  (`tests/policy/test_confirmations.py`), `SENSITIVE_TOOLS` (`tests/security/test_registry.py`),
  `CUSTOM_CALLS` (`tests/contract/test_spec_contract.py`), `DEGRADED_ON_VANILLA`
  (`tests/docker/conftest.py`). `EXEMPT_FROM_STAND` остался пустым.
- **Интеграционный ярус:** `tests/docker/test_tag_custom.py` — 6 новых тестов, каждый новый
  тул вызывается против настоящего сервера (сторож полноты удовлетворён);
  `test_fork_write_routes_are_absent_on_the_vanilla_stand` каждый прогон переспрашивает
  сервер, что путей нет. **Локально не прогонялись: colima не запущена, стенда нет.**
- `ruff check tag_mcp tests scripts`, `mypy tag_mcp`, `scripts/build_spec.py --check` — чисто.
- `docs/TOOLS.md` перегенерирован скриптом: **72 → 77** инструментов.

## Коммиты

```
271fe79 feat(posts): I-7 8  — vote_in_poll
ffdc1a8 feat(posts): I-7 9  — revoke_poll_vote
da6fdef feat(posts): I-7 10 — close_poll
bf4706c feat(posts): I-7 11 — forward_posts
fb4d397 feat(posts): I-7 12 — mark_post_read
a2c49e4 test(docker): I-7 13 — пять пишущих тулов против настоящего сервера
```

## Открытые вопросы

- Тело `POST /posts/forward` (`{post_ids, channel_id, message}`) собрано по имени метода
  `Client4` и настройкам стенда: пишущие пути форка при разведке не вызывались, дампов нет.
  Ответ сервера тул не разбирает — результат говорит о том, что отправлено, а не что вернулось.
  Первая же проверка на живом ТЭГ либо подтвердит форму, либо потребует правки.
- `README.md` и `docs/ARCHITECTURE.md` всё ещё называют 66 тулов — расхождение возникло
  на приоритете 2 и не трогалось здесь.
- Приоритет 4 карты (достижения, настройки канала, закреплённые каналы) и блок D ТЗ
  (сужение разбора по дампам) не начинались.

## Дополнение оркестратора (2026-08-25)

Интеграционный ярус, который dev-агент не смог прогнать (не был поднят стенд), прогнан:
`MM_DOCKER_TESTS=1 TESTCONTAINERS_RYUK_DISABLED=true` — **29 passed за 44 с** (23 прежних + 6 новых),
включая сторож полноты и проверку отсутствия путей форка на ванильном Mattermost 11.9.

Грабли локального прогона: colima + testcontainers — реапер (ryuk) монтирует docker.sock по пути
`~/.colima/default/docker.sock`, которого нет в VM → 500 на старте любого контейнера.
Обход: `TESTCONTAINERS_RYUK_DISABLED=true` (уборка и так делается контекст-менеджерами).
В GitLab CI проблема не существует — там dind (`DOCKER_HOST=tcp://docker:2375`).

Также приведены счётчики документации: 66 → 77 курируемых тулов (magnit-tag-mcp, коммит 19e5d42).
