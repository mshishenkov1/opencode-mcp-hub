# Отчёт TEST-агента: ревизия 4.1 — сирота выпущенного токена (AC-232…AC-237) и гейт G3

Ветка: `pipeline/i3-hub-oauth-facade-proxy`.
Спека: §31 spec.md (правило R-U19, решения 99–103), коммит `7ea3d38`.
Реализация: `aad383a` (broker/api/db), `797ff05` (`also_copy` шаблонов для mutmut).
Тесты: `b403b2a` (общие помощники), `6d9ddf9` (`tests/test_orphan_issued_token.py`).

## 1. Таблица AC → тест → результат

| AC | Правило | Тесты (`tests/test_orphan_issued_token.py`) | Случаев | Результат |
|---|---|---|---|---|
| AC-232 | R-U19.1–R-U19.4, R-U19.7, доп. R-U14.3 | `test_orphan_is_revoked_before_hub_forgets_its_id`, `test_revoke_block_is_taken_from_the_issuing_method` | 2 | passed |
| AC-233 | R-U19.3, R-U19.4, R-U19.7 | `test_fallback_attempt_with_submitted_token_succeeds` (а), `test_both_revoke_attempts_fail_and_leave_a_cleanup_mark` (б) | 2 | passed |
| AC-234 | R-U19.6, уточнение R-U15.4 | `test_successful_reconnect_picks_up_the_mark` (а), `test_repeated_failed_connect_retries_the_mark` (б), `test_disconnect_picks_up_the_mark` (в) | 3 | passed |
| AC-235 | R-U19.5, R-U19.7, R-U19.8 | `test_nothing_to_revoke_with_keeps_the_mark_and_records_the_fact` (а), `test_disconnect_with_unenforceable_mark_still_succeeds` (б) | 2 | passed |
| AC-236 | уточнение R-U16 (решение 103) | `test_flag_is_published_for_unavailable_method_but_block_is_not` | 1 | passed |
| AC-237 | R-U19.8, доп. R-U14.3 | `test_issued_token_id_disappears_only_on_confirmed_revoke` (прогоны 1–5), `test_method_without_exchange_keeps_the_mark` (прогон 6) | 6 | passed |

Покрыты **все шесть критериев** ревизии 4.1: 16 тестовых функций, 16 прогоняемых случаев.

## 2. Что именно закреплено

* **Не более двух запросов отзыва за заход (R-U19.3).** AC-232 — один запрос, если первый удался;
  AC-233 — ровно два и не больше, причём первый уходит с прежним постоянным токеном подключения
  (`Bearer PERMANENT-1`), а запасной — с присланным (`Bearer SESSION-2`). Проверяется точный список
  `revoke_credentials`, поэтому ни перестановка попыток, ни лишний повтор мимо не пройдут.
* **Блок берётся у способа, которым токен был выпущен.** Отдельный тест: пользователь
  переподключается **другим** способом, у которого обмена нет вовсе. Если бы Hub смотрел на текущий
  способ, отзывать было бы нечем и сирота осталась бы; тест требует, чтобы отзыв всё равно ушёл.
* **Исход подключения не меняется (R-U19.4).** Во всех сценариях ответ 200, `status: connected`,
  а `token_origin`/`token_origin_reason` — те, что предписывает R-U14.3, независимо от исхода уборки.
* **Пометка на уборку.** Пара «`token_origin: submitted` при непустом `issued_token_id`» проверяется
  прямым чтением строки `upstream_tokens`; AC-237 перебирает шесть переходов и требует, чтобы
  непустой идентификатор исчезал **только** по подтверждённому отзыву или по замене новым выпущенным.
* **Одно событие аудита на процедуру.** `upstream_token_revoked` пишется один раз по итоговому
  исходу, а не на каждый из двух запросов: AC-233(а) — одно событие с `outcome: ok` при двух
  запросах; AC-233(б) — одно с `outcome: failed` плюс один `upstream_token_cleanup_failed` со
  `stage: revoke`. Наборы `details` сверяются равенством, а не вхождением.
* **Отзывать нечем (R-U19.5).** Каталог перечитывается через `/admin/catalog/reload` уже без блока
  `exchange` — проверяется, что не ушло ни одного запроса (ни отзыва, ни списка, ни выпуска), пометка
  цела, в аудите `stage: orphan`, а в журнале запись **WARNING** с `alias` и стадией.
* **Отключение (уточнение R-U15.4, R-U19.8).** С исполнимой пометкой — ровно один запрос отзыва;
  с неисполнимой — ноль запросов, но строка всё равно удаляется, подключение переходит в
  `not_connected`, пишутся `connection_disconnected` и `upstream_token_cleanup_failed`. Присланный
  токен не отзывается ни в одном прогоне, OAuth-`revoke_url` не вызывается.
* **Секреты (R-U17.3).** Значения токенов ищутся по записям **всех** логгеров всех уровней
  (`capture_all_levels` + `all_log`), открыто хранимый `issued_token_id` — по журналу Hub (`hub_log`):
  он законно виден в DEBUG-эхе SQL драйвера БД. Тот же разбор ревью приняло для AC-224. Дополнительно
  проверено, что `tokid-1` не встречается ни в одном `details` аудита, ни в ответах API.

## 3. Итоги прогонов

| Что | Команда | Результат |
|---|---|---|
| Новые тесты | `.venv/bin/pytest tests/test_orphan_issued_token.py -q` | 16 passed |
| Полный сьют | `.venv/bin/pytest tests/ -q` | **852 passed, 0 failed**, 1 deselected (`load`) |
| Линтер | `.venv/bin/ruff check src tests` | All checks passed |
| Типы | `.venv/bin/mypy src` | Success: no issues found in 39 source files |
| Трассировка (G6) | `.venv/bin/python scripts/check_ac_traceability.py` | **G6 OK: все 237 критериев покрыты тестами** |

Прирост относительно ревизии 4: 836 → 852 (+16 случаев).

## 4. Гейт G3 (mutation): полный прогон с нуля

`mutants/` удалён целиком, дерево мутантов сгенерировано заново из текущих исходников
(в `mutants/src/hub/broker.py` присутствуют `revoke_orphan_issued_token` и `clear_issued_token_id`,
то есть код ревизии 4.1 действительно мутировался). Команда — `no_proxy='*' .venv/bin/mutmut run`,
сводка снята `mutmut export-cicd-stats` (`mutants/mutmut-cicd-stats.json`).

| Показатель | Значение |
|---|---|
| Всего мутантов | 12 002 |
| 🎉 killed | 8 965 |
| ⏰ timeout | 2 |
| 🙁 survived | 892 |
| 🤔 suspicious | 0 |
| 🫥 no tests (нет покрывающих тестов) | 2 143 |
| **Мутационный балл** | **91.0 %** (detected 8 967 / 9 859), порог **70 %** → гейт пройден |

Балл считается формулой `scripts/run_gates.py`: `(killed + timeout) / (killed + timeout + survived +
suspicious)`; мутанты «no tests» в знаменатель не входят. Предыдущий зелёный прогон G3
(`reports/gates-i3-plus-h5-20260820.json`) давал 74.4 % — ревизии 4 и 4.1 подняли балл до 91.0 %.

### 4.1. Выжившие в новом коде ревизий 4 и 4.1

| Файл | Что учтено | Мутантов | Выжило |
|---|---|---|---|
| `src/hub/broker.py` | 23 функции обмена, уборки, отзыва сироты и срока годности | 816 | **0** |
| `src/hub/routes/api.py` | весь файл (эндпоинт подключения, шаги R-U13, отключение) | 329 | **0** |
| `src/hub/catalog.py` | `_check_templates`, `_env_ref_allowed`, `public_auth_methods` | 78 | **0** |
| `src/hub/web.py` | `format_moment`, `token_origin_notice` | 29 | **0** |
| `src/hub/routes/web.py` | `_token_rows`, `_origin_notice`, `ui_server`, `ui_connections` | 20 | **0** |
| | **ИТОГО** | **1 272** | **0** |

**Выживших мутантов в коде ревизий 4 и 4.1 нет ни одного** — ни в путях обмена
(`exchange_user_token`, `_upstream_json`, классификация причин), ни в уборке
(`cleanup_issued_tokens`, `_list_issued_tokens`, маркер), ни в уборке сироты ревизии 4.1
(`revoke_orphan_issued_token`, `previous_issued_token`, `clear_issued_token_id`, `save_tokens`,
`delete_tokens`), ни в чтении срока годности (`read_submitted_expiry`, `_parse_expiry_moment`).
Разбирать на эквивалентность и дописывать тесты нечего.

### 4.2. Где живут 892 выживших

Все они — в коде прежних итераций, вне объёма ревизий 4 и 4.1:

| Модуль | Выжило | Модуль | Выжило |
|---|---|---|---|
| `routes/mcp.py` | 155 | `litellm.py` | 17 |
| `broker.py` (старые функции: `refresh`, `mark_needs_reauth`, исключения) | 150 | `oauth.py` | 16 |
| `cli.py` | 123 | `kv.py` | 15 |
| `oidc.py` | 117 | `routes/oauth.py` | 15 |
| `routes/web.py` (вход и веб-сессии) | 77 | `web.py` (заголовки пресетов и групп) | 13 |
| `proxy.py` | 55 | `app.py` | 10 |
| `login.py` | 46 | прочие (9 модулей) | 28 |
| `metrics.py` | 30 | | |
| `websession.py` | 25 | **ВСЕГО** | **892** |

Замечание для оркестратора: 150 выживших в `broker.py` относятся к функциям вне ревизий 4/4.1
(`refresh`, `mark_needs_reauth`, конструкторы исключений) — их разбор лежит вне этого задания, но
это самая крупная концентрация выживших в модуле, который ревизия 4 активно расширяла, и его стоит
запланировать отдельной задачей. Гейт при этом пройден с запасом: 91.0 % против порога 70 %.

## 5. Замечания по ходу работы

* Помощники перехвата журнала (`capture_all_levels`, `all_log`, `hub_log`) переехали из
  `tests/test_token_exchange.py` в `tests/support.py`: ими пользуются тесты обеих ревизий, а правило
  «значения токенов ищем по всем логгерам, открыто хранимый идентификатор — по журналу Hub» должно
  быть описано в одном месте.
* `MockTokenApi` получил `revoke_responder` (ответ по самому запросу, а не очередью — отзыв сироты
  идёт двумя учётными данными подряд) и `reset_requests` (отделяет подготовку состояния от предмета
  проверки).
* Правка `797ff05` (`also_copy` шаблонов Jinja) подтверждена этим прогоном: полный прогон mutmut
  прошёл целиком — сбор статистики, мутанты, сводка, — без падения на устаревших шаблонах, которое
  описано в `reports/test-report-rev4.md`, §8.
