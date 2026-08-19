# Отчёт TEST-AGENT — итерация 2 (режим `MM_HTTP_AUTH=oauth`)

Репозиторий: `magnit-tag-mcp`, ветка `feature/oauth-mode`.
Вход: `docs/oauth-mode/spec.md`, `docs/oauth-mode/acceptance-criteria.yaml` (AC-01…AC-79).
Тесты: `tests/oauth/` (13 файлов + `conftest.py`), маркеры `@pytest.mark.ac("AC-NN")`
зарегистрированы в `tests/conftest.py::pytest_configure`.

## 1. Числа

| Показатель | Значение |
| --- | --- |
| Собрано тестов во всём наборе | 931 |
| Прошло | 875 |
| Упало | 1 (`test_default_allow_list_rejects_dot_segments_in_redirect_path`, оставлен красным — BUG-I2-001) |
| Пропущено | 55 (docker / load / live — выключены переменными окружения, как и раньше) |
| Тестов режима oauth (`tests/oauth/`) | 195 |
| Критериев приёмки | 79 (AC-01…AC-79) |
| AC без теста | 0 |
| AC без **зелёного** теста | 0 |
| Покрытие `tag_mcp/` | 100,00 % (3064 оператора, 0 непокрытых) |
| `ruff check tag_mcp tests scripts` | чисто |
| `mypy tag_mcp` | чисто (32 файла) |

Тесты по файлам: `settings` 26, `endpoints` 23, `registration` 32, `authorize` 19,
`consent` 15, `callback_token` 15, `mcp_access` 9, `refresh_revoke` 10,
`scopes_policy` 12, `storage_logs` 6, `delivery` 10, `edge_branches` 18.

Команды (окружение `export PATH=/opt/homebrew/bin:$PATH`):

```
uv run pytest -n auto -q                                          # 1 failed, 875 passed, 55 skipped
uv run pytest --cov=tag_mcp --cov-report=term-missing -q -p no:randomly
uv run ruff check tag_mcp tests scripts
```

## 2. Решение по падавшему тесту: `code_bug`, тест оставлен красным

`tests/oauth/test_registration.py::test_default_allow_list_rejects_dot_segments_in_redirect_path`.

AC-26 перечисляет `https://x.example/a/../b` среди URI, которые `POST /register`
обязан отклонить с `400 invalid_redirect_uri`; R-23 говорит прямо: «URI со схемами
`javascript|data|file|vbscript`, с userinfo или dot-сегментами отвергаются всегда»,
R-24 — что проверка выполняется в том числе при DCR. Фактически сервер отвечает
`201` и сохраняет клиента с `redirect_uris = ["https://x.example/b"]`.

Тест не ошибочен: требование в спецификации сформулировано именно так, и
собственный валидатор проекта этот URI отвергает — соседний тест
`test_allow_list_function_rejects_the_same_uris[https://x.example/a/../b]`
(прямой вызов `tag_mcp.oauth_redirects.redirect_uri_allowed`) зелёный. Разрыв —
на границе HTTP: `redirect_uris` в `OAuthClientInformationFull` типизированы
`pydantic.AnyUrl`, который схлопывает `/a/../b` в `/b` ещё при разборе тела,
и `TagOAuthProxy.register_client` (`tag_mcp/oauth.py:460`) видит уже
нормализованное значение. Правка — в коде продукта, трогать его нельзя, поэтому
оформлен баг, а тест оставлен падающим как воспроизведение.

## 3. Баги

| ID | AC | Severity | Симптом | Падающий тест | Статус |
| --- | --- | --- | --- | --- | --- |
| BUG-I2-001 | AC-26 (R-23, R-24) | low | `POST /register` принимает `redirect_uri` с dot-сегментами: `201` и молчаливая нормализация вместо `400 invalid_redirect_uri` | `tests/oauth/test_registration.py::test_default_allow_list_rejects_dot_segments_in_redirect_path` | open |

Почему `low`, а не выше: эксплуатируемого обхода allow-list за нормализацией не
нашлось — сохраняется и используется уже нормализованный URI, и он проходит те же
шаблоны. Проверено отдельно: при `TAG_MCP_ALLOWED_REDIRECTS='https://*.magnit.ru/safe/*'`
URI `https://app.magnit.ru/safe/../evil` нормализуется в `https://app.magnit.ru/evil`
и отвергается (нормализация здесь закрывает обход, а не открывает). Расхождение —
в наблюдаемом контракте `/register`, описанном в AC-26.

Файл: `bugs/BUG-I2-001.json`.

## 4. Покрытие

`uv run pytest --cov=tag_mcp --cov-report=term-missing -q -p no:randomly`:

```
TOTAL                              3064      0   100%
Required test coverage of 100.0% reached. Total coverage: 100.00%
```

Непокрытых строк нет ни в одном модуле, включая новые `oauth.py` (355),
`oauth_redirects.py` (40), `oauth_ui.py` (16), `scopes.py` (34). ~22 строки,
остававшиеся непокрытыми на момент прошлого обрыва, закрыты тестами поведения в
`tests/oauth/test_edge_branches.py` и `test_registration.py`: не-JSON ответ
Mattermost на обмене кода, чужие тела в хуке снятия `scope`, `OPTIONS`-preflight
через обёртки `/register`, `/token`, `/revoke`, не-HTTP scope в этих же обёртках,
переписывание `401 invalid_grant` в `400` (в т. ч. ответ кусками и не-JSON),
`revoke` с пустым `client_secret` и с токеном без гранта, рассинхрон допустимых
scopes между рестартами, не-200 ответ библиотеки на экране согласия,
синтаксически негодные URI в allow-list, `reset_scopes()`.

### Строки, исключённые из измерения (`# pragma: no cover` в коде продукта)

Их поставил DEV-AGENT; тестовому агенту править `tag_mcp/` нельзя, поэтому просто
называю их — метрика 100 % считается без них:

| Место | Ветка | Почему недостижима из тестов |
| --- | --- | --- |
| `tag_mcp/oauth.py:458` | `return client` в `get_client` | Достижимо только если FastMCP вернёт клиента, который не `ProxyDCRClient` и не `TagDCRClient`; библиотека такого объекта не отдаёт, подсунуть его можно лишь патчем внутренностей — это был бы тест реализации, а не поведения из AC |
| `tag_mcp/oauth.py:543` | `mapping is None` в `_remember_grant` | Маппинг `jti → upstream` пишется в том же вызове строкой выше; ветка срабатывает только при потере записи между двумя обращениями к хранилищу |
| `tag_mcp/oauth.py:826` | `metadata is None` в `get_routes` | SDK всегда создаёт маршрут `/.well-known/oauth-authorization-server`; отсутствие означало бы смену контракта библиотеки |
| `tag_mcp/httpauth.py:218`, `tag_mcp/middleware.py:108` | защитные проверки | Были в коде до итерации, приняты ранее |

Плюс два блока `if TYPE_CHECKING:` — исключение настроено в `pyproject.toml` и в этой
итерации не менялось.

## 5. Стабильность

Полный прогон `uv run pytest -n auto -q` выполнен дважды подряд (плагин
`pytest-randomly` каждый раз тасует порядок новым seed): оба раза
`1 failed, 875 passed, 55 skipped`, 22,5 с и 23,5 с. Тесты, помеченные
`@pytest.mark.flaky_suspect`, отсутствуют — подозрений на нестабильность нет.
Красный тест один и тот же в обоих прогонах, то есть это детерминированное
расхождение с AC, а не флак.

## 6. Правки этой итерации

* Дооформлен триаж падающего теста → `bugs/BUG-I2-001.json`; сам тест не менялся.
* Приведён к чистому `ruff check`: убраны 10 неактуальных директив `# noqa`
  (`S105`/`SLF001` не включены для `tests/`) в `tests/oauth/conftest.py`,
  `test_edge_branches.py`, `test_mcp_access.py`, `test_storage_logs.py` и
  переименованы три неиспользуемых распакованных переменных (RUF059) в
  `test_authorize.py`, `test_callback_token.py`, `test_edge_branches.py`.
  Поведение тестов не изменилось.
* Код продукта (`tag_mcp/`) не трогался; ранее принятые тесты не ослаблялись и не
  удалялись; `pyproject.toml`, `.claude/`, `.github/` не менялись.

## 7. Таблица AC → тесты

Собрана скриптом по маркерам `@pytest.mark.ac(...)` (pytest-плагин на
`pytest_collection_modifyitems`), 195 привязок, все 79 критериев закрыты.
Имена файлов — сокращённо, без префикса `tests/oauth/test_` и расширения;
`×N` — число параметризаций.

| AC | Критерий | Тесты (`tests/oauth/test_*.py`) | Всего | Статус |
| --- | --- | --- | --- | --- |
| AC-01 | Старт в режиме oauth без обязательных переменных падает с ошибкой, перечисляющей все недостающие | `settings`: test_missing_all_required_variables_are_listed_together<br>`settings`: test_create_server_also_refuses_without_required_variables | 2 | зелено |
| AC-02 | Отсутствие ровно одной обязательной переменной называется по имени | `settings`: test_single_missing_variable_is_named_and_others_are_not<br>`settings`: test_blank_value_counts_as_missing ×2 | 3 | зелено |
| AC-03 | Ключ подписи короче 32 байт отвергается на старте | `settings`: test_signing_key_shorter_than_32_bytes_is_rejected<br>`settings`: test_signing_key_of_exactly_32_bytes_is_accepted | 2 | зелено |
| AC-04 | TAG_MCP_PUBLIC_URL нормализуется и валидируется | `settings`: test_public_url_is_trimmed_and_stripped_of_trailing_slash<br>`settings`: test_public_url_without_scheme_or_with_query_is_rejected ×3 | 4 | зелено |
| AC-05 | http:// в TAG_MCP_PUBLIC_URL допускается с предупреждением | `settings`: test_http_public_url_is_allowed_but_warned_about<br>`settings`: test_https_public_url_does_not_warn_about_cookies | 2 | зелено |
| AC-06 | Хранилище: неверная схема — ошибка, отсутствие — предупреждение об in-memory | `edge_branches`: test_blank_storage_url_means_memory<br>`settings`: test_storage_url_with_foreign_scheme_is_rejected<br>`settings`: test_missing_storage_url_means_memory_store_with_a_warning<br>`settings`: test_redis_schemes_are_accepted ×2 | 5 | зелено |
| AC-07 | Неверные TAG_MCP_DEFAULT_SCOPES и TAG_MCP_CONSENT отвергаются на старте | `settings`: test_invalid_default_scopes_and_consent_are_rejected_by_name ×3<br>`settings`: test_admin_default_scope_needs_admin_enabled | 4 | зелено |
| AC-08 | Другие режимы не требуют oauth-переменных и не меняют своих проверок | `settings`: test_passthrough_needs_no_oauth_variables<br>`settings`: test_token_mode_keeps_its_own_check<br>`settings`: test_other_modes_ignore_broken_oauth_values ×3 | 5 | зелено |
| AC-09 | В режиме oauth сервер поднимается без MM_TOKEN и не ходит в Mattermost при старте | `endpoints`: test_server_starts_without_mm_token_and_does_not_call_mattermost | 1 | зелено |
| AC-10 | MCP-эндпоинт без токена отвечает 401 с указателем на метаданные ресурса | `endpoints`: test_mcp_without_valid_token_answers_401_with_resource_metadata ×2 | 2 | зелено |
| AC-11 | Метаданные защищённого ресурса указывают на сам сервер | `endpoints`: test_protected_resource_metadata_points_to_this_server | 1 | зелено |
| AC-12 | Метаданные authorization server объявляют все эндпоинты и S256 | `endpoints`: test_authorization_server_metadata_declares_endpoints_and_s256<br>`endpoints`: test_metadata_with_path_prefix_is_also_served_path_aware | 2 | зелено |
| AC-13 | Только /mcp требует Bearer-токен | `edge_branches`: test_options_preflight_passes_through_the_body_guards ×3<br>`endpoints`: test_only_mcp_requires_a_bearer_token ×10 | 13 | зелено |
| AC-14 | URL в метаданных не зависят от заголовка Host | `endpoints`: test_metadata_urls_ignore_the_host_header<br>`endpoints`: test_resource_metadata_ignores_the_host_header | 2 | зелено |
| AC-15 | Динамическая регистрация клиента выдаёт публичного клиента | `registration`: test_registration_issues_independent_public_clients | 1 | зелено |
| AC-16 | Регистрация с недопустимым redirect URI отклоняется и не сохраняется | `registration`: test_registration_with_a_foreign_redirect_is_rejected_and_not_stored<br>`registration`: test_authorize_rejects_a_foreign_redirect_even_for_the_upstream_client | 2 | зелено |
| AC-17 | Регистрация с недопустимым scope отклоняется | `registration`: test_registration_with_unknown_scope_is_rejected<br>`registration`: test_registration_with_valid_scope_subset_is_accepted | 2 | зелено |
| AC-18 | Клиент без scope получает tag:read | `authorize`: test_client_without_scope_gets_tag_read<br>`authorize`: test_empty_scope_parameter_means_the_default | 2 | зелено |
| AC-19 | TAG_MCP_DEFAULT_SCOPES меняет умолчание | `authorize`: test_default_scopes_setting_changes_the_default<br>`authorize`: test_explicit_scope_overrides_the_default | 2 | зелено |
| AC-20 | Запрос недопустимого scope отклоняется без создания транзакции | `authorize`: test_unknown_scope_is_rejected_without_a_transaction<br>`authorize`: test_admin_scope_is_accepted_when_admin_is_enabled<br>`edge_branches`: test_scope_allowed_at_registration_but_not_now_is_rejected | 3 | зелено |
| AC-21 | authorize с чужим redirect_uri отклоняется | `authorize`: test_foreign_redirect_uri_is_refused_without_redirect ×3<br>`authorize`: test_other_loopback_port_is_accepted | 4 | зелено |
| AC-22 | Несовпадающий resource отвергается | `authorize`: test_foreign_resource_is_rejected_with_invalid_target<br>`authorize`: test_own_resource_is_accepted ×3 | 4 | зелено |
| AC-23 | authorize ведёт на экран согласия либо сразу в Mattermost по TAG_MCP_CONSENT | `authorize`: test_remember_and_always_lead_to_the_consent_screen ×2<br>`authorize`: test_external_goes_straight_to_mattermost | 3 | зелено |
| AC-24 | URL Mattermost по умолчанию не содержит scope, PKCE и resource | `authorize`: test_default_mattermost_url_has_no_scope_pkce_or_resource | 1 | зелено |
| AC-25 | Настройки пробрасывания PKCE и upstream-scope действуют | `authorize`: test_forward_pkce_and_upstream_scope_reach_mattermost | 1 | зелено |
| AC-26 | Умолчание TAG_MCP_ALLOWED_REDIRECTS: loopback любой порт и любой https | `edge_branches`: test_malformed_uris_never_match ×4<br>`registration`: test_default_allow_list_accepts_loopback_and_https ×5<br>`registration`: test_default_allow_list_rejects_everything_else ×5<br>`registration`: test_default_allow_list_rejects_dot_segments_in_redirect_path<br>`registration`: test_allow_list_function_rejects_the_same_uris ×6 | 21 | 20 зелёных, 1 красный (BUG-I2-001) |
| AC-27 | Собственный список шаблонов и пустое значение | `registration`: test_custom_patterns_replace_the_default<br>`registration`: test_empty_allow_list_means_the_default<br>`registration`: test_any_host_pattern_semantics ×6 | 8 | зелено |
| AC-28 | Отказы логируются событием oauth.reject без секретов | `registration`: test_rejections_are_logged_without_secrets<br>`registration`: test_no_reject_event_for_a_good_registration | 2 | зелено |
| AC-29 | Экран согласия — на русском, с названием ТЭГ MCP, клиентом, redirect и кнопками | `consent`: test_consent_screen_is_russian_and_names_everything<br>`consent`: test_consent_form_posts_to_the_same_path | 2 | зелено |
| AC-30 | Каждый запрошенный scope показан текстом из маппинга | `consent`: test_every_requested_scope_is_explained_in_human_words<br>`consent`: test_client_without_a_name_is_shown_by_client_id<br>`consent`: test_docs_use_the_same_scope_texts_as_the_screen | 3 | зелено |
| AC-31 | Разрешить — редирект в Mattermost с cookie привязки | `consent`: test_approve_redirects_to_mattermost_with_a_binding_cookie | 1 | зелено |
| AC-32 | Отмена — редирект клиенту с access_denied, гранта нет | `consent`: test_deny_returns_access_denied_to_the_client_and_no_grant | 1 | зелено |
| AC-33 | remember: повторное подключение того же клиента проходит без экрана | `consent`: test_remember_skips_the_screen_for_the_same_client_in_the_same_browser<br>`consent`: test_remember_does_not_apply_to_another_browser | 2 | зелено |
| AC-34 | always: экран показывается каждый раз | `consent`: test_always_shows_the_screen_every_time | 1 | зелено |
| AC-35 | Отмена не запоминается | `consent`: test_denial_is_not_remembered<br>`consent`: test_forged_denied_cookie_is_ignored | 2 | зелено |
| AC-36 | Каждое решение на экране согласия логируется | `consent`: test_every_consent_decision_is_logged | 1 | зелено |
| AC-37 | Неверные транзакция или CSRF отвергаются | `consent`: test_bad_transaction_or_csrf_is_refused<br>`consent`: test_unknown_action_is_refused<br>`edge_branches`: test_non_200_library_consent_response_is_passed_through | 3 | зелено |
| AC-38 | Callback обменивает код у Mattermost методом client_secret_post и отдаёт клиенту наш код | `callback_token`: test_callback_exchanges_the_code_with_client_secret_post<br>`callback_token`: test_upstream_claims_are_optional_when_mattermost_is_silent | 2 | зелено |
| AC-39 | Сломанный callback не создаёт гранта | `callback_token`: test_broken_callbacks_create_no_grant<br>`edge_branches`: test_non_json_token_response_from_mattermost_fails_the_callback_cleanly | 2 | зелено |
| AC-40 | Поле scope в ответах Mattermost не влияет на грант | `callback_token`: test_scope_from_mattermost_does_not_affect_the_grant ×2<br>`edge_branches`: test_scope_stripping_hook_tolerates_foreign_bodies | 3 | зелено |
| AC-41 | Ответ /token содержит только FastMCP-токены | `callback_token`: test_token_response_contains_only_our_tokens | 1 | зелено |
| AC-42 | PKCE, одноразовость и принадлежность кода проверяются | `callback_token`: test_pkce_one_time_use_and_client_binding_are_enforced<br>`callback_token`: test_token_without_client_id_is_a_400_not_401<br>`callback_token`: test_redirect_uri_must_match_the_authorize_request<br>`edge_branches`: test_guards_are_transparent_for_non_http_scopes<br>`edge_branches`: test_invalid_grant_rewrite_handles_chunked_and_non_json_401 | 5 | зелено |
| AC-43 | expires_in берётся из Mattermost, иначе из TAG_MCP_FALLBACK_ACCESS_TTL | `callback_token`: test_expires_in_comes_from_mattermost_or_the_fallback ×2 | 2 | зелено |
| AC-44 | Claims FastMCP JWT | `callback_token`: test_jwt_claims<br>`callback_token`: test_jwt_signed_with_another_key_is_not_accepted<br>`callback_token`: test_refresh_jwt_is_not_accepted_as_an_access_token<br>`edge_branches`: test_upstream_claims_are_absent_without_an_access_token | 4 | зелено |
| AC-45 | Внутри вызова тула AccessToken отражает подключение | `mcp_access`: test_access_token_inside_a_tool_reflects_the_connection | 1 | зелено |
| AC-46 | Недействительные JWT и отвергнутые upstream-токены дают 401 | `mcp_access`: test_invalid_jwts_and_rejected_upstream_tokens_give_401<br>`mcp_access`: test_jwt_for_another_audience_is_rejected<br>`mcp_access`: test_jti_mapping_is_what_binds_the_jwt_to_the_grant | 3 | зелено |
| AC-47 | Истекающий upstream-токен обновляется прозрачно | `mcp_access`: test_expiring_upstream_token_is_refreshed_transparently | 1 | зелено |
| AC-48 | Неудачный прозрачный refresh — 401 | `mcp_access`: test_failed_transparent_refresh_is_401 | 1 | зелено |
| AC-49 | Refresh ротирует токены | `refresh_revoke`: test_refresh_rotates_both_tokens<br>`refresh_revoke`: test_old_access_token_keeps_working_after_refresh | 2 | зелено |
| AC-50 | Refresh не расширяет, но может сузить scopes | `refresh_revoke`: test_refresh_cannot_widen_but_can_narrow_scopes | 1 | зелено |
| AC-51 | Refresh JWT привязан к клиенту | `refresh_revoke`: test_refresh_token_is_bound_to_its_client<br>`refresh_revoke`: test_refresh_without_client_id_is_400 | 2 | зелено |
| AC-52 | Отзыв access JWT завершает весь грант и вызывает logout в Mattermost | `refresh_revoke`: test_revoking_the_access_token_ends_the_whole_grant | 1 | зелено |
| AC-53 | Отзыв refresh JWT, отключённый и упавший logout | `refresh_revoke`: test_revoking_refresh_token_disabled_logout_and_failed_logout | 1 | зелено |
| AC-54 | Неизвестный или чужой токен на /revoke не ломает чужие гранты | `edge_branches`: test_revoke_with_an_explicit_empty_client_secret_is_accepted<br>`edge_branches`: test_revoke_token_ignores_tokens_without_a_grant<br>`refresh_revoke`: test_unknown_or_foreign_token_on_revoke_does_not_break_other_grants<br>`refresh_revoke`: test_revoke_without_client_id_is_400<br>`refresh_revoke`: test_revoke_accepts_a_public_client_without_client_secret_field | 5 | зелено |
| AC-55 | authorize и выдача токена логируются | `callback_token`: test_authorize_and_token_are_logged | 1 | зелено |
| AC-56 | tag:admin объявляется только при MM_ENABLE_ADMIN | `endpoints`: test_admin_scope_is_advertised_only_with_enable_admin | 1 | зелено |
| AC-57 | tools/list фильтруется scopes подключения | `scopes_policy`: test_tools_list_is_filtered_by_connection_scopes | 1 | зелено |
| AC-58 | В passthrough пустые scopes не ограничивают тулы | `edge_branches`: test_reset_scopes_clears_the_binding<br>`scopes_policy`: test_passthrough_has_no_scope_filter | 2 | зелено |
| AC-59 | Глобальная политика — потолок для scopes | `scopes_policy`: test_global_policy_is_the_ceiling | 1 | зелено |
| AC-60 | Профиль lite, списки каналов и подтверждения действуют вместе со scopes | `scopes_policy`: test_lite_profile_channel_lists_and_confirmations_still_apply | 1 | зелено |
| AC-61 | Грант без tag:read прячет read-тулы | `scopes_policy`: test_grant_without_tag_read_hides_read_tools | 1 | зелено |
| AC-62 | Вызов тула, скрытого по scopes, — ошибка forbidden с подсказкой | `scopes_policy`: test_calling_a_hidden_tool_is_forbidden_with_a_hint | 1 | зелено |
| AC-63 | Скрытие глобальной политикой проверяется раньше scopes | `scopes_policy`: test_global_policy_error_comes_before_scopes | 1 | зелено |
| AC-64 | Запись в личный или групповой чат требует tag:dm | `scopes_policy`: test_writing_to_direct_or_group_needs_tag_dm | 1 | зелено |
| AC-65 | Чтение личного чата покрывается tag:read | `scopes_policy`: test_reading_a_direct_channel_needs_only_tag_read | 1 | зелено |
| AC-66 | Порядок проверок: списки каналов раньше scopes, scopes раньше подтверждения | `scopes_policy`: test_channel_lists_go_before_scopes_and_scopes_before_confirmation<br>`scopes_policy`: test_policy_check_order_at_the_unit_level | 2 | зелено |
| AC-67 | whoami показывает scopes подключения только в oauth | `scopes_policy`: test_whoami_reports_granted_scopes_only_in_oauth | 1 | зелено |
| AC-68 | FastMCP JWT никогда не уходит в Mattermost | `mcp_access`: test_our_jwt_never_reaches_mattermost | 1 | зелено |
| AC-69 | Подключения изолированы | `mcp_access`: test_connections_are_isolated<br>`mcp_access`: test_tools_list_differs_per_grant_on_the_same_server | 2 | зелено |
| AC-70 | Две реплики над одним хранилищем проходят флоу без sticky-сессий | `storage_logs`: test_two_replicas_share_one_store_without_sticky_sessions | 1 | зелено |
| AC-71 | Upstream-токены в хранилище зашифрованы, смена ключа не роняет сервер | `storage_logs`: test_upstream_tokens_are_encrypted_and_key_rotation_does_not_crash<br>`storage_logs`: test_storage_builder_wraps_the_backend_in_encryption | 2 | зелено |
| AC-72 | Без TAG_MCP_STORAGE_URL флоу работает в памяти | `storage_logs`: test_full_flow_works_in_memory_without_storage_url | 1 | зелено |
| AC-73 | Зависимость redis входит в проект | `endpoints`: test_redis_dependency_is_declared_and_importable<br>`endpoints`: test_redis_storage_url_does_not_touch_the_network_at_startup | 2 | зелено |
| AC-74 | /ready отражает доступность хранилища | `endpoints`: test_ready_reflects_storage_health<br>`endpoints`: test_ready_does_not_need_a_process_identity | 2 | зелено |
| AC-75 | В логах есть все события и нет секретов | `storage_logs`: test_all_events_are_logged_and_no_secret_leaks<br>`storage_logs`: test_library_logs_at_info_and_above_carry_no_secrets_either | 2 | зелено |
| AC-76 | Существующие тесты проходят без правок | `delivery`: test_previous_modes_keep_their_behaviour<br>`delivery`: test_legacy_test_suites_are_still_present_and_untouched_by_oauth_markers | 2 | зелено |
| AC-77 | Compose-профиль и пример окружения для oauth | `delivery`: test_compose_has_an_oauth_profile_with_redis<br>`delivery`: test_env_example_documents_oauth_variables<br>`delivery`: test_dockerfile_base_image_is_unchanged | 3 | зелено |
| AC-78 | Документация режима oauth | `delivery`: test_doc_describes_admin_steps_and_variables<br>`delivery`: test_doc_describes_client_connection<br>`delivery`: test_doc_has_scope_table_ceiling_and_order | 3 | зелено |
| AC-79 | Качество: моки, покрытие, линтеры | `delivery`: test_quality_gates_are_configured<br>`delivery`: test_oauth_tests_never_touch_the_network | 2 | зелено |
