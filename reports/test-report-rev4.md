# Отчёт TEST-агента: ревизия 4 — обмен присланного токена на постоянный (AC-214…AC-231)

Ветка: `pipeline/i3-hub-oauth-facade-proxy`.
Правила: §27 spec.md (R-U12…R-U18, R-U10.1), контракты §28, критерии AC-214…AC-231.

## 1. Таблица AC → тест → результат

| AC | Правило | Тесты | Случаев | Результат |
|---|---|---|---|---|
| AC-214 | R-U13 | `tests/test_token_exchange.py::test_exchange_stores_issued_token_not_the_submitted_one` | 1 | passed |
| AC-215 | R-U13, R-P2 | `tests/test_token_exchange.py::test_proxy_sends_issued_token_and_never_the_submitted_one` | 1 | passed |
| AC-216 | R-U12, R-C1 | `tests/test_token_exchange.py::test_exchange_schema_is_checked_with_paths` (случаи а–к), `…::test_method_without_exchange_keeps_previous_behaviour` | 11 | passed |
| AC-217 | R-U14 | `tests/test_token_exchange.py::test_policy_denied_connects_with_submitted_token` (403, 501, 400, 404) | 4 | passed |
| AC-218 | R-U14 | `tests/test_token_exchange.py::test_unusable_issue_response_connects_with_submitted_token` (13 исходов), `…::test_numeric_token_id_is_accepted` (граница «число годится») | 14 | passed |
| AC-219 | R-U13.4, R-U14 | `tests/test_token_exchange.py::test_unusable_issued_token_is_revoked_and_connection_stays_submitted` (401, 500, другой аккаунт) | 3 | passed |
| AC-220 | R-U15 | `tests/test_token_exchange.py::test_reconnect_does_not_multiply_personal_tokens` | 1 | passed |
| AC-221 | R-U15.2, R-U15.3 | `…::test_cleanup_revokes_only_exact_marker_matches`, `…::test_cleanup_failure_does_not_break_the_connection` (list/revoke), `…::test_cleanup_revokes_at_most_twenty_items_per_pass` | 4 | passed |
| AC-222 | R-U15.4, R-U5 | `tests/test_token_exchange.py::test_disconnect_revokes_only_the_token_hub_issued` (issued / submitted / отзыв 500) | 3 | passed |
| AC-223 | R-U13, R-U17 | `tests/test_token_exchange.py::test_submitted_token_is_not_stored_anywhere_after_successful_exchange` | 1 | passed |
| AC-224 | R-U17 | `tests/test_token_exchange.py::test_issued_token_id_and_response_body_never_leak` | 1 | passed |
| AC-225 | R-U16 | `tests/test_token_origin_view.py::test_permanent_and_session_connections_look_different`, `…::test_connect_form_of_exchanging_method_explains_permanent_token` | 2 | passed |
| AC-226 | R-U15.1 | `tests/test_token_exchange.py::test_marker_isolates_hub_installations` | 1 | passed |
| AC-227 | R-U16, R-U8, R-C6 | `tests/test_token_origin_view.py::test_only_the_flag_is_published_not_the_exchange_block`, `tests/test_token_exchange.py::test_server_without_auth_methods_is_untouched` | 2 | passed |
| AC-228 | R-U10.1 | `…::test_tag_catalog_entry_of_revision_4_loads_and_works`, `…::test_tag_catalog_entry_of_revision_4_survives_forbidden_exchange` | 2 | passed |
| AC-229 | R-U14.4 | `tests/test_token_exchange.py::test_failed_exchange_is_not_remembered` | 1 | passed |
| AC-230 | R-U18 | `tests/test_token_expiry.py::test_submitted_token_expiry_is_the_upper_bound`, `…::test_issued_token_never_asks_for_session_expiry`, `…::test_expires_unit_is_honoured` (s, iso8601), `…::test_invalid_expiry_block_is_schema_error` (3 случая), `…::test_expiry_on_oauth_method_is_schema_error` | 8 | passed |
| AC-231 | R-U18.4 | `tests/test_token_expiry.py::test_unreadable_expiry_invents_no_date` (8 исходов) | 8 | passed |

Покрыты **все 18 критериев** ревизии 4 (AC-214…AC-231): 30 тестовых функций, 68 прогоняемых
случаев в трёх новых файлах.

## 2. Что именно проверено по «особому вниманию»

* **Судьба присланного токена (R-U13, AC-223).** После удавшегося обмена ни одного фрагмента
  присланного значения (длиной 8 и более символов) нет: в дампе всех таблиц схемы, в кэше
  `conn:<user_id>:<alias>`, в `audit_log` (включая `details`), в теле ответов
  `POST …/token`, `/api/me/connections`, `/api/catalog`, в HTML `/ui/servers/{alias}` и в журнале
  Hub любого уровня. Одновременно расшифровка `access_token_enc` даёт постоянный токен.
* **Отказ обмена (AC-217, AC-218).** 17 исходов: понятный отказ политики (403/501/400/404) даёт
  `policy_denied`, недоступность и непригодный ответ (в том числе пустое/нестроковое `token_field`,
  отсутствующий и пустой `token_id_field`, не-JSON, не-объект, код, не равный `expect_status`) —
  `upstream_unavailable`. Во всех случаях ответ 200, подключение создано присланным токеном
  целиком (частичной записи нет: расшифровка даёт ровно присланное значение, `issued_token_id`
  пуст), запросы к `exchange.list` и `exchange.revoke` не отправлялись.
* **Отзыв постоянного токена при отключении (R-U15.4, AC-222).** Для `issued` — ровно один запрос
  на `exchange.revoke` с сохранённым `token_id`, отправленный сохранённым постоянным токеном; для
  `submitted` — ни одного; при отказе отзыва (500) отключение всё равно успешно. OAuth-`revoke_url`
  не вызывается ни в одном случае.
* **Срок годности присланного токена (R-U18, AC-230/AC-231).** Берётся наибольший срок в будущем
  (179 дней), а не ближайший, не `0` и не прошедший; значение совпадает в ответе, в
  `upstream_tokens.submitted_expires_at` и на странице сервера с формулировкой «не позднее».
  При `token_origin: issued` запрос сессий не отправляется вовсе.
* **Недоступность обмена и отсутствие пригодных элементов (AC-231).** Восемь исходов чтения срока
  (403, 500, сеть, таймаут, не-JSON, пустой список, все `0`, все в прошлом) — `session_expires_at`
  и `submitted_expires_at` остаются `null`, подключение `connected`, на странице предупреждение
  без даты, тело ответа целевой системы в журнал не попало.

## 3. Итоги прогона

| Что | Команда | Результат |
|---|---|---|
| Затронутые файлы | `.venv/bin/pytest tests/test_token_exchange.py tests/test_token_expiry.py tests/test_token_origin_view.py -q` | 68 passed |
| Полный сьют | `.venv/bin/pytest tests/ -x -q` | **830 passed, 0 failed**, 1 deselected (`load`) |
| Покрытие | `.venv/bin/python -m pytest -q --cov=src …` | **93 %** по `src` |
| Линтер | `.venv/bin/ruff check src tests` | без замечаний |

Покрытие ключевых модулей ревизии 4: `hub/broker.py` 88 %, `hub/catalog.py` 96 %,
`hub/routes/api.py` 97 %, `hub/web.py` 96 %, `hub/routes/web.py` 90 %, `hub/db.py` 97 %.

Тестов, помеченных `flaky_suspect`, нет: нестабильных падений при повторных прогонах не
наблюдалось (время берётся из `ManualClock`, сеть — из `MockNetwork`).

## 4. Найденные дефекты

**Новых дефектов не заведено.** Реализация коммитов `69e7927`, `5750f21`, `a232899` по всем
восемнадцати критериям соответствует §27 spec.md; расхождений, требующих баг-репорта, не найдено.

## 5. Приведение прежних тестов к контракту ревизии 4

Пять ранее принятых тестов проверяли **закрытые** наборы ключей и head-ревизию, которые ревизия 4
расширяет по §28 / R-U16 / R-U17.4. Наборы остались закрытыми — в них добавлены ровно новые ключи,
и добавлены проверки их значений (ослабления проверок нет):

| Тест | Что изменилось |
|---|---|
| `tests/test_api.py::test_me_connections` (AC-56) | набор ключей элемента дополнен `token_origin`, `token_origin_reason`, `session_expires_at`; добавлена проверка, что у OAuth-подключения все три `null` |
| `tests/test_broker.py::test_provider_tokens_never_leak_outside` (AC-113) | тот же набор ключей |
| `tests/test_user_token.py::test_success_response_shape_has_no_token` (AC-176) | набор ключей ответа `POST …/token` дополнен теми же тремя; добавлена проверка `submitted`/`null` у способа без `exchange` |
| `tests/test_user_token.py::test_catalog_publishes_methods_without_secrets` (AC-185) | публичный вид способа дополнен `issues_permanent_token`; добавлена проверка, что у способов без `exchange` он `false` |
| `tests/test_migrations.py::test_head_revision_is_i3` (AC-138) | head и цепочка ревизий дополнены `0004_i4_token_exchange` |

## 6. Инфраструктура тестов (`tests/support.py`)

* `MockTokenApi` — мок личных токенов целевой системы: выпуск, список, отзыв и сессии на четырёх
  адресах, с собственной очередью сценария на каждый запрос и учётом состава списка токенов
  учётной записи (выпуск добавляет элемент, отзыв удаляет). Зарегистрирован в `MockNetwork`
  дважды: на тестовых адресах и на адресах записи `tag` из R-U10.1.
* `MockVerify.by_token` — ответ проверки по значению токена: шаг 2 и шаг 4 R-U13 выполняются одним
  и тем же блоком `verify`, и сценарий «выпущенный токен непригоден» задаётся именно так;
  `MockVerify.tokens_seen()` — порядок значений, с которыми выполнялась проверка.
* `exchange_block()`, `expiry_block()`, `tag_spec_server_rev4()` — блоки каталога ревизии 4.

Отдельная оговорка по AC-224: `caplog.set_level(DEBUG)` включает отладочный вывод драйвера БД
(`aiosqlite` печатает текст SQL вместе со значениями параметров, среди которых `issued_token_id` —
он по R-U17.3 хранится открытым). Это вывод сторонней библиотеки, а не журнал Hub, поэтому
проверка утечек в AC-224 идёт по записям логгера `hub` всех уровней (вспомогательная
функция `_hub_log`); прочие проверки утечек (AC-223, AC-231) — по всем записям.
