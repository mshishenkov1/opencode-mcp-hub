# Отчёт TEST-агента: ревизия 4 — обмен присланного токена на постоянный (AC-214…AC-231)

Ветка: `pipeline/i3-hub-oauth-facade-proxy`.
Правила: §27 spec.md (R-U12…R-U18, R-U10.1), контракты §28, критерии AC-214…AC-231.

## 1. Таблица AC → тест → результат

| AC | Правило | Тесты | Случаев | Результат |
|---|---|---|---|---|
| AC-214 | R-U13 | `tests/test_token_exchange.py::test_exchange_stores_issued_token_not_the_submitted_one`, `…::test_rejected_submitted_token_never_reaches_the_issue_request` (401, 403, 500, таймаут) | 5 | passed |
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

Покрыты **все 18 критериев** ревизии 4 (AC-214…AC-231): 31 тестовая функция, 72 прогоняемых
случая в трёх новых файлах. Дополнительно ревизию 4 закрывают два теста миграции в
`tests/test_migrations.py` (AC-142, см. §7).

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
| Затронутые файлы | `.venv/bin/pytest tests/test_token_exchange.py tests/test_token_expiry.py tests/test_token_origin_view.py -q` | 72 passed |
| Полный сьют | `.venv/bin/pytest tests/ -q` | **836 passed, 0 failed**, 1 deselected (`load`) |
| Покрытие | `.venv/bin/python -m pytest -q --cov=src …` | **93 %** по `src` |
| Линтер | `.venv/bin/ruff check src tests` | без замечаний |
| Типы | `.venv/bin/mypy src` | Success: no issues found in 39 source files |
| Мутационный гейт | `no_proxy='*' .venv/bin/mutmut run` | доходит до конца сбора статистики и завершается, exit 0 (см. §8) |

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

Отдельная оговорка по AC-224 — см. §7: значения токенов и тело ответа на выпуск проверяются по
записям **всех** логгеров, и только открыто хранимый `issued_token_id` — по журналу Hub.

---

# Итерация 2: правки по ревью `reports/review-rev4-1.json` (request_changes)

## 7. Что исправлено

### 7.1 MUST_FIX — проверка утечек в журнал была вырождена на уровне DEBUG

Замечание подтверждено экспериментально. `caplog.set_level(logging.DEBUG)` поднимает только
root-логгер; логгер `hub` к этому моменту уже возвращён на INFO вызовом `configure_logging`
внутри `create_app` (autouse-фикстура `hub_logs_captured` отрабатывает раньше приложения).
Замер после создания приложения: `logging.getLogger("hub").level == 20`,
`isEnabledFor(DEBUG) is False`.

Правка — вспомогательная функция `_capture_all_levels(caplog)` в `tests/test_token_exchange.py`
и та же пара вызовов в `tests/test_token_expiry.py`: сначала `caplog.set_level(DEBUG)` (сторонние
библиотеки), затем `caplog.set_level(DEBUG, logger="hub")` (журнал Hub), строго **после**
создания приложения.

Фальсифицируемость проверена инъекцией: `TokenBroker.exchange_user_token` через `monkeypatch`
пишет значение присланного токена в `hub.broker`, после чего проверка утечки должна падать.

| Уровень записи | До правки | После правки |
|---|---|---|
| `logger.debug` | утечка **не поймана** | утечка поймана |
| `logger.info` | утечка поймана | утечка поймана |

Затронуты четыре места из ревью: AC-221 (`test_cleanup_failure_does_not_break_the_connection`),
AC-223, AC-224 и AC-231 (`test_unreadable_expiry_invents_no_date`).

Дополнительно та же вырожденность устранена в двух унаследованных тестах с тем же given
(«журнал любого уровня»), которых ревью не касалось: `tests/test_user_token.py::
test_user_token_never_appears_in_logs_or_audit` (AC-188) и `tests/test_require_account.py::
test_rejection_does_not_log_response_body` (AC-202). Оба остаются зелёными — это усиление
проверки, а не ослабление.

### 7.2 Рекомендация по AC-224 принята

`test_issued_token_id_and_response_body_never_leak` теперь делит проверку:

* `PERMANENT-SECRET-8` и `BODY-MARKER-8` — по записям **всех** логгеров (`_all_log`), чтобы фильтр
  не замаскировал утечку значения постоянного токена или тела ответа через стороннюю библиотеку;
* `tokid-SECRET-ID` — по журналу Hub (`_hub_log`): идентификатор по R-U17.3 не учётные данные,
  хранится открытым и законно виден в DEBUG-эхе SQL драйвера БД, а правило запрещает его именно
  в журнале Hub.

Проверки AC-221, AC-223 и AC-231 переведены на `_all_log` — фильтра по логгеру в них больше нет.

### 7.3 Миграция 0004 на существующих строках `upstream_tokens`

Добавлены два теста в `tests/test_migrations.py` (маркер AC-142):

* `test_revision_4_keeps_previous_behaviour_for_existing_tokens` — БД доводится до
  `0003_i4_user_token`, засевается строка `upstream_tokens` с шифртекстом, затем `upgrade` до head:
  `token_origin == 'submitted'`, `issued_token_id`, `token_origin_reason` и
  `submitted_expires_at` — `NULL`, `access_token_enc` и `token_type` не изменились (R-U17.4,
  решение 95). Перед апгрейдом проверяется, что четырёх колонок ещё нет — иначе проверка была бы
  вырожденной;
* `test_revision_4_downgrade_drops_columns_without_data_loss` — `downgrade` до `0003` снимает ровно
  четыре колонки (`before - after == set(NEW_COLUMNS)`), строка не теряется, повторный `upgrade`
  возвращает прежний вид: миграция обратима.

### 7.4 R-U13 шаг 2 — отвергнутый токен не уходит в запрос выпуска

Добавлен `tests/test_token_exchange.py::test_rejected_submitted_token_never_reaches_the_issue_request`
(маркер AC-214), четыре случая: verify отвечает 401 и 403 → 400 `token_rejected`; 500 и таймаут →
502 `upstream_unavailable`. Во всех случаях `issue_requests`, `list_requests`, `revoke_requests` и
`sessions_requests` мока пусты, строки `upstream_tokens` нет, статус подключения не `connected`.

## 8. Гейт G3 (mutation): причина найдена, тест исправления не требует

Гипотеза ревью — «хрупкая проверка текста страницы, чувствительная к преобразованию mutmut» — **не
подтвердилась**. `test_both_scopes_pass_authorize_and_give_readonly_preset` к преобразованию не
чувствителен: все 187 отобранных mutmut тестов, включая его, проходят против преобразованных
исходников (`mutants/src` в `sys.path`, `MUTANT_UNDER_TEST=stats`) — 187 passed, exit 0.

Причина — **устаревшие данные пакета в артефакте `mutants/`**, а не тест:

1. `copy_src_dir()` (`mutmut/__main__.py:195`) копирует файл только если целевого ещё нет
   (`if target_path.exists(): continue`). Файлы `.py` пересоздаются каждым прогоном, а
   не-Python данные пакета — **никогда**. В `mutants/src/hub/templates/` лежали копии от 19 августа;
   от рабочего дерева отличались три: `consent.html`, `server.html`, `connections.html`.
   `consent.html` не содержал блока `scope_is_set` (ревизия 3.2), поэтому страница согласия под
   mutmut отдавалась без пояснения — и тест падал **правомерно**. `server.html` и
   `connections.html` изменены ревизией 4 (a232899), то есть по той же причине упали бы и
   AC-225/AC-227 — ревью их не увидело из-за `-x`. Сам mutmut это диагностирует: печатает список
   изменившихся не-Python файлов и советует удалить `mutants/`.
2. После обновления шаблонов оставалась вторая поломка: `mutants/mutmut-stats.json` от прежней
   редакции уводил прогон на инкрементальную ветку `collect_or_load_stats` («Found 187 new tests,
   rerunning stats collection»). Это **второй** в том же процессе вызов `pytest.main`, уже со
   списком node id, — он собирает ноль тестов («no tests ran») и возвращает 4, что mutmut
   превращает в `BadTestExecutionCommandsException`. Тот же список аргументов в свежем процессе
   даёт 187 passed, то есть дело не в тестах и не в исходниках.

Сделано (обе правки — внутри `mutants/`, каталог в `.gitignore`, это артефакт сборки, не код):
удалены устаревшие не-Python копии в `mutants/src` и `mutants/mutmut-stats.json`. После этого
`no_proxy='*' .venv/bin/mutmut run` **доходит до конца сбора статистики** («Running clean tests»,
«Running forced fail test», «done») и завершается штатно: «Running mutation testing», `exit 0`.

Ни тест, ни продуктовый код при этом не менялись: ослаблять проверку `«все объявленные области» in
page.text` было бы неверно — она поймала настоящее расхождение между тем, что рендерится под
mutmut, и текущим шаблоном.

**Что осталось за пределами зоны test-агента (для dev/оркестратора):**

* мутационный балл ревизии 4 этим прогоном **не измерен**: mutmut отчитался «0 files mutated,
  0 ignored, 39 unmodified» и `0/0` — результаты мутантов взяты из кэша прошлого полного прогона.
  Чтобы получить свежий балл, нужен полный прогон с пересозданием `mutants/` (долгий) — это
  задание гейта G3, а не правка тестов;
* чтобы устаревание не повторялось, данные пакета следует добавить в `also_copy`
  (`[tool.mutmut]` в `pyproject.toml`): `also_copy = ["catalog.yaml", "deploy", "src/hub/templates"]`.
  `copy_also_copy_files()` использует `copytree(..., dirs_exist_ok=True)` и перезаписывает файлы
  каждый прогон, в отличие от `copy_src_dir()`. `pyproject.toml` — зона dev-агента, поэтому правка
  оставлена ему.

## 9. Итоги итерации 2

| Что | Результат |
|---|---|
| Полный сьют | **836 passed, 0 failed**, 1 deselected (`load`) |
| Прирост | +6 случаев: 4 — AC-214 (шаг 2 R-U13), 2 — AC-142 (миграция 0004) |
| `ruff check src tests` | All checks passed |
| `mypy src` | Success: no issues found in 39 source files |
| `mutmut run` | сбор статистики пройден целиком, прогон завершается, exit 0 |
| Новых дефектов | нет; продуктовый код не менялся |
