# Отчёт TEST-агента — итерация I-3 (ревизия 2)

Ветка: `pipeline/i3-hub-oauth-facade-proxy`. Критерии AC-70…AC-149 (80 новых), правила spec.md
часть II §9–19 (R-T*, R-O*, R-B*, R-P*, R-W*, R-M*, R-N*).

> Разделы 1–7 — прогон до ревью. Итог после `reports/review-i3-1.json` и ревизии 2.1 spec.md
> (AC-150…AC-156, уточнённые AC-73/74/83/98/141/145/148) — в **разделе 8**: 633 теста, 156 AC.

## 1. Итоги прогона

| Показатель | Значение |
|---|---|
| Тестов в сьюте | **612** (408 от I-1 + **204** новых) + 1 нагрузочный под маркером `load` |
| Прошло / упало | 612 / 0 (два последовательных прогона и прогон без кэша — стабильно) |
| Skip / xfail | 0 |
| Deselected | 1 (`tests/test_load_sse.py::test_hundred_parallel_sse_streams`, маркер `load`) |
| `ruff check src tests` | чисто |
| `scripts/check_ac_traceability.py` | G6 OK: все 149 критериев покрыты |
| Покрытие `src/` (строки) | **93 %** |
| `diff-cover` к `main` | **92 %** (порог 90 %) |
| Багов заведено | **0** (ни одно падение не квалифицировано как `code_bug`) |
| Помечено `flaky_suspect` | 0 |

Команды: `.venv/bin/python -m pytest -q -rs --tb=short`,
`.venv/bin/python -m pytest -q --cov=src --cov-report=xml:reports/coverage.xml --cov-report=term`,
`.venv/bin/diff-cover reports/coverage.xml --compare-branch=main --fail-under=90`,
`.venv/bin/ruff check src tests`, `python3 scripts/check_ac_traceability.py`.
Нагрузочный smoke: `.venv/bin/python -m pytest -q -m load` (1 passed, 612 deselected).

## 2. Новые файлы тестов

| Файл | Тестов | Область |
|---|---|---|
| `tests/test_settings_i3.py` | 20 | настройки R-T1..R-T4, `deploy/.env.example` (AC-70…AC-73, AC-145) |
| `tests/test_oauth_as.py` | 66 | Hub как authorization server R-O1..R-O13 (AC-75…AC-101, AC-148) |
| `tests/test_broker.py` | 29 | брокер токенов целевых систем R-B1..R-B9 (AC-102…AC-113) |
| `tests/test_proxy.py` | 28 | MCP-proxy R-P1..R-P11 (AC-114…AC-121, AC-125…AC-130) |
| `tests/test_permissions.py` | 7 | фильтр инструментов R-P8 (AC-122…AC-124) |
| `tests/test_web.py` | 27 | веб-интерфейс R-W1..R-W6 (AC-131…AC-137) |
| `tests/test_migrations.py` | 18 | модель данных и миграции R-M1..R-M5 (AC-138…AC-142) |
| `tests/test_observability_i3.py` | 5 | метрики, аудит, организация тестов R-N1..R-N4 (AC-143, AC-144, AC-146) |
| `tests/test_e2e_i3.py` | 4 | сквозной сценарий и восстановление (AC-74, AC-147, AC-149) |
| `tests/test_load_sse.py` | 1 (`load`) | 100 одновременных SSE-потоков (AC-146) |

Инфраструктура (`tests/support.py`, `tests/conftest.py`):

* `MockUpstream` — upstream MCP streamable-http: `initialize` (выдаёт `Mcp-Session-Id`), `tools/list`,
  `tools/call`, ответы `application/json` и `text/event-stream`, 401/404/405/5xx, таймаут и сетевая
  ошибка, `DELETE`; фиксирует метод, адрес, заголовки и тело каждого запроса;
* `MockProviderAS` — AS целевой системы: обмен кода, refresh с ротацией и без неё, `400 invalid_grant`,
  `500`, сетевая ошибка, `revoke`; проверяются `client_id`/`client_secret`/`redirect_uri`/`code_verifier`;
* `MockOIDC` — discovery, JWKS, `token` с подписанным `id_token` (RS256, joserfc), ветки «чужой ключ»,
  «чужой nonce», «чужой issuer», «истёк», «нет id_token», «провайдер недоступен»;
* `MockNetwork` — единый `httpx.MockTransport` для `http_client` (upstream + AS + OIDC): любой запрос
  к неизвестному адресу — `AssertionError` (сеть в тестах невозможна, AC-146); LiteLLM — respx из I-1;
* `asgi_stream(...)` — прямой вызов ASGI-приложения с потоковым чтением тела и имитацией разрыва
  соединения (`httpx.ASGITransport` буферизует ответ целиком и для AC-116/AC-126 непригоден);
* сценарные помощники: `register_client`, `web_login`, `litellm_web_login`, `authorize_to_code`,
  `submit_consent`, `provider_callback`, `exchange_code`, `refresh_grant`, `seed_connection`,
  `connected_client`, `i3_catalog`, `pkce_pair`, `tamper_signature`;
* `pytest_collection_modifyitems` в `tests/conftest.py` исключает маркер `load` из обычного прогона
  (именно исключает, а не пропускает: в отчёте нет ни skip, ни xfail).

## 3. Трассировка AC → тесты

| AC | Тестов | Тесты |
|---|---|---|
| AC-70 | 4 | `test_default_access_token_ttl_is_one_hour` (test_settings_i3.py); `test_default_consent_is_always` (test_settings_i3.py); `test_default_web_auth_is_litellm` (test_settings_i3.py); `test_default_tools_cache_ttl_serves_second_call_from_cache` (test_settings_i3.py) |
| AC-71 | 3 | `test_invalid_new_settings_break_start` (test_settings_i3.py); `test_choice_settings_list_allowed_values` (test_settings_i3.py); `test_valid_new_settings_start` (test_settings_i3.py) |
| AC-72 | 4 | `test_keycloak_mode_requires_issuer` (test_settings_i3.py); `test_keycloak_mode_requires_client_secret` (test_settings_i3.py); `test_keycloak_mode_with_both_starts` (test_settings_i3.py); `test_litellm_mode_needs_no_keycloak_vars` (test_settings_i3.py) |
| AC-73 | 2 | `test_i1_environment_serves_i3_endpoints` (test_settings_i3.py); `test_i1_defaults_of_new_settings` (test_settings_i3.py) |
| AC-74 | 2 | `test_revision2_secrets_do_not_leak` (test_e2e_i3.py); `test_needs_reauth_page_has_no_tokens` (test_e2e_i3.py) |
| AC-75 | 2 | `test_as_metadata_matches_rfc8414` (test_oauth_as.py); `test_as_metadata_needs_no_authentication` (test_oauth_as.py) |
| AC-76 | 2 | `test_as_metadata_with_resource_suffix` (test_oauth_as.py); `test_as_metadata_suffix_404_for_non_facade` (test_oauth_as.py) |
| AC-77 | 1 | `test_protected_resource_metadata` (test_oauth_as.py) |
| AC-78 | 2 | `test_mcp_without_bearer_returns_401_with_resource_metadata` (test_oauth_as.py); `test_mcp_with_malformed_authorization_returns_401` (test_oauth_as.py) |
| AC-79 | 2 | `test_prm_404_for_native_unconfigured_unknown` (test_oauth_as.py); `test_proxy_404_for_native_unconfigured_unknown` (test_oauth_as.py) |
| AC-80 | 1 | `test_dynamic_registration_returns_public_client` (test_oauth_as.py) |
| AC-81 | 2 | `test_register_rejects_bad_redirect_uri` (test_oauth_as.py); `test_register_rejects_bad_metadata` (test_oauth_as.py) |
| AC-82 | 1 | `test_register_rate_limited_per_ip` (test_oauth_as.py) |
| AC-83 | 3 | `test_authorize_unknown_client_shows_page` (test_oauth_as.py); `test_authorize_rejects_other_clients_redirect` (test_oauth_as.py); `test_authorize_without_redirect_uri_shows_page` (test_oauth_as.py) |
| AC-84 | 1 | `test_authorize_errors_redirect_with_state` (test_oauth_as.py) |
| AC-85 | 1 | `test_authorize_without_session_redirects_to_login_and_back` (test_oauth_as.py) |
| AC-86 | 3 | `test_alias_from_resource_defaults_to_readonly` (test_oauth_as.py); `test_alias_from_scope_without_resource` (test_oauth_as.py); `test_alias_conflict_or_absence_is_invalid_request` (test_oauth_as.py) |
| AC-87 | 1 | `test_authorize_runs_provider_oauth_then_consent` (test_oauth_as.py) |
| AC-88 | 4 | `test_consent_remember_skips_screen` (test_oauth_as.py); `test_consent_always_shows_screen_again` (test_oauth_as.py); `test_consent_remember_asks_again_for_other_scope` (test_oauth_as.py); `test_remembered_consent_row_is_updated` (test_oauth_as.py) |
| AC-89 | 4 | `test_consent_deny_returns_access_denied` (test_oauth_as.py); `test_consent_with_expired_transaction` (test_oauth_as.py); `test_consent_of_other_user_is_forbidden` (test_oauth_as.py); `test_consent_without_session_is_forbidden` (test_oauth_as.py) |
| AC-90 | 2 | `test_code_is_single_use_and_revokes_chain` (test_oauth_as.py); `test_expired_code_is_rejected` (test_oauth_as.py) |
| AC-91 | 1 | `test_code_exchange_issues_token_pair` (test_oauth_as.py) |
| AC-92 | 1 | `test_wrong_or_missing_verifier_rejected_and_code_survives` (test_oauth_as.py) |
| AC-93 | 1 | `test_code_exchange_checks_client_and_redirect` (test_oauth_as.py) |
| AC-94 | 1 | `test_access_token_claims` (test_oauth_as.py) |
| AC-95 | 1 | `test_refresh_rotates_pair` (test_oauth_as.py) |
| AC-96 | 1 | `test_refresh_reuse_revokes_whole_chain` (test_oauth_as.py) |
| AC-97 | 1 | `test_revoke_always_returns_200` (test_oauth_as.py) |
| AC-98 | 1 | `test_token_signature_expiry_and_audience` (test_oauth_as.py) |
| AC-99 | 2 | `test_hot_path_uses_cache_and_denylist` (test_oauth_as.py); `test_permission_change_invalidates_connection_cache` (test_oauth_as.py) |
| AC-100 | 1 | `test_oauth_errors_follow_rfc6749` (test_oauth_as.py) |
| AC-101 | 1 | `test_token_rate_limit_per_client_and_ip` (test_oauth_as.py) |
| AC-102 | 2 | `test_provider_scopes_follow_preset_and_pkce` (test_broker.py); `test_missing_provider_secret_fails_exchange` (test_oauth_as.py) |
| AC-103 | 7 | `test_callback_without_state_is_rejected` (test_broker.py); `test_callback_with_foreign_state_is_rejected` (test_broker.py); `test_callback_state_is_single_use` (test_broker.py); `test_callback_from_other_web_session_is_rejected` (test_broker.py); `test_callback_with_provider_error_returns_access_denied` (test_broker.py); `test_callback_without_code_is_invalid_request` (test_oauth_as.py); `test_callback_with_provider_failure_shows_page` (test_oauth_as.py) |
| AC-104 | 3 | `test_provider_tokens_stored_encrypted` (test_broker.py); `test_provider_response_without_access_token_fails` (test_broker.py); `test_provider_network_error_fails_exchange` (test_broker.py) |
| AC-105 | 5 | `test_background_refresh_runs_ahead_and_locks` (test_broker.py); `test_background_refresh_skips_fresh_token` (test_broker.py); `test_background_refresh_picks_due_connection` (test_broker.py); `test_background_refresh_survives_provider_failure` (test_broker.py); `test_refresher_start_and_stop_are_idempotent` (test_broker.py) |
| AC-106 | 2 | `test_upstream_401_triggers_refresh_and_single_retry` (test_broker.py); `test_second_upstream_401_gives_connection_error` (test_broker.py) |
| AC-107 | 1 | `test_refresh_failure_marks_needs_reauth` (test_broker.py) |
| AC-108 | 1 | `test_expired_token_without_refresh_needs_reauth` (test_broker.py) |
| AC-109 | 2 | `test_provider_refresh_rotation_is_optional` (test_broker.py); `test_token_without_expires_in_stays_valid` (test_broker.py) |
| AC-110 | 1 | `test_permission_change_applies_to_next_call` (test_broker.py) |
| AC-111 | 4 | `test_upgrade_to_readwrite_requires_reauth` (test_broker.py); `test_unknown_and_denied_groups_rejected` (test_broker.py); `test_readonly_preset_drops_readwrite_group` (test_broker.py); `test_consent_readwrite_repeats_provider_oauth` (test_oauth_as.py) |
| AC-112 | 3 | `test_disconnect_revokes_provider_and_client_tokens` (test_broker.py); `test_disconnect_without_revoke_url_still_works` (test_broker.py); `test_disconnect_of_missing_connection_is_404` (test_broker.py) |
| AC-113 | 1 | `test_provider_tokens_never_leak_outside` (test_broker.py) |
| AC-114 | 2 | `test_initialize_and_tools_list_are_proxied` (test_proxy.py); `test_non_json_upstream_body_is_passed_through` (test_proxy.py) |
| AC-115 | 1 | `test_upstream_headers_are_rewritten` (test_proxy.py) |
| AC-116 | 6 | `test_sse_response_is_streamed` (test_proxy.py); `test_upstream_timeout_returns_502` (test_proxy.py); `test_get_returns_sse_stream` (test_proxy.py); `test_get_passes_through_non_sse_response` (test_proxy.py); `test_sse_idle_timeout_closes_stream` (test_proxy.py); `test_network_error_returns_502` (test_proxy.py) |
| AC-117 | 2 | `test_client_sees_hub_session_id` (test_proxy.py); `test_session_of_other_user_is_not_found` (test_proxy.py) |
| AC-118 | 2 | `test_idle_upstream_session_is_recreated` (test_proxy.py); `test_recreation_failure_returns_upstream_error` (test_proxy.py) |
| AC-119 | 2 | `test_upstream_404_triggers_single_recreation` (test_proxy.py); `test_repeated_404_after_recreation_gives_upstream_error` (test_proxy.py) |
| AC-120 | 3 | `test_delete_closes_session` (test_proxy.py); `test_delete_without_session_header_is_404` (test_proxy.py); `test_delete_survives_upstream_error` (test_proxy.py) |
| AC-121 | 2 | `test_tools_cache_ttl_permissions_and_reload` (test_proxy.py); `test_tools_list_with_cursor_is_not_cached` (test_proxy.py) |
| AC-122 | 4 | `test_tools_list_hides_unavailable_tools_json` (test_permissions.py); `test_tools_list_hides_unavailable_tools_sse` (test_permissions.py); `test_repo_write_group_shows_its_tools` (test_permissions.py); `test_catalog_without_filters_shows_all_tools` (test_permissions.py) |
| AC-123 | 2 | `test_forbidden_tools_call_is_rejected` (test_permissions.py); `test_allowed_tools_call_reaches_upstream` (test_permissions.py) |
| AC-124 | 1 | `test_batch_with_forbidden_call_is_rejected` (test_permissions.py) |
| AC-125 | 1 | `test_rate_limit_per_user_and_alias` (test_proxy.py) |
| AC-126 | 2 | `test_concurrent_sse_streams_are_limited` (test_proxy.py); `test_sse_counter_released_on_client_disconnect` (test_proxy.py) |
| AC-127 | 2 | `test_large_body_with_content_length_is_rejected` (test_proxy.py); `test_large_chunked_body_is_rejected` (test_proxy.py) |
| AC-128 | 1 | `test_circuit_breaker_opens_and_recovers` (test_proxy.py) |
| AC-129 | 1 | `test_missing_connection_returns_jsonrpc_error` (test_proxy.py) |
| AC-130 | 1 | `test_revoked_and_expired_tokens_are_rejected` (test_proxy.py) |
| AC-131 | 7 | `test_oidc_login_creates_web_session` (test_web.py); `test_oidc_callback_with_foreign_state_fails` (test_web.py); `test_external_next_is_replaced` (test_web.py); `test_oidc_provider_unavailable_shows_error` (test_web.py); `test_oidc_callback_error_and_missing_code` (test_web.py); `test_oidc_token_without_id_token_fails` (test_web.py); `test_oidc_login_updates_existing_user` (test_web.py) |
| AC-132 | 1 | `test_invalid_id_token_creates_no_session` (test_web.py) |
| AC-133 | 5 | `test_litellm_web_login_creates_session` (test_web.py); `test_login_with_active_session_redirects_to_next` (test_web.py); `test_login_start_failure_shows_error_page` (test_web.py); `test_login_poll_states` (test_web.py); `test_login_team_selection_fragment` (test_web.py) |
| AC-134 | 1 | `test_consent_screen_shows_catalog_groups_and_saves_choice` (test_web.py) |
| AC-135 | 1 | `test_connections_page_shows_only_own_connections` (test_web.py) |
| AC-136 | 3 | `test_server_card_hides_internal_data` (test_web.py); `test_server_card_requires_session` (test_web.py); `test_server_card_visible_to_audience_group` (test_web.py) |
| AC-137 | 6 | `test_consent_without_csrf_is_forbidden` (test_web.py); `test_permissions_put_requires_csrf_with_cookie` (test_web.py); `test_bearer_key_needs_no_csrf` (test_web.py); `test_logout_clears_session` (test_web.py); `test_html_responses_have_charset_and_cache_control` (test_web.py); `test_logout_without_session_redirects_to_login` (test_web.py) |
| AC-138 | 5 | `test_migrations_applied_at_startup` (test_migrations.py); `test_restart_keeps_schema_and_data` (test_migrations.py); `test_auto_migrate_disabled_keeps_schema_empty` (test_migrations.py); `test_cli_db_upgrade_and_current` (test_migrations.py); `test_head_revision_is_i3` (test_migrations.py) |
| AC-139 | 2 | `test_new_tables_have_required_columns` (test_migrations.py); `test_new_tables_unique_constraints` (test_migrations.py) |
| AC-140 | 2 | `test_connections_extended_without_breaking_i1` (test_migrations.py); `test_connection_revision_defaults_to_zero_and_grows` (test_migrations.py) |
| AC-141 | 2 | `test_kv_atomic_primitives` (test_migrations.py); `test_revision2_kv_keys_and_ttls` (test_migrations.py) |
| AC-142 | 2 | `test_i1_database_is_upgraded_without_data_loss` (test_migrations.py); `test_current_revision_is_none_for_empty_database` (test_migrations.py) |
| AC-143 | 1 | `test_revision2_metrics_are_exposed` (test_observability_i3.py) |
| AC-144 | 1 | `test_audit_records_revision2_actions_without_secrets` (test_observability_i3.py) |
| AC-145 | 2 | `test_env_example_lists_all_new_settings` (test_settings_i3.py); `test_env_example_keeps_secrets_empty` (test_settings_i3.py) |
| AC-146 | 4 | `test_hundred_parallel_sse_streams` (test_load_sse.py); `test_outgoing_traffic_is_intercepted` (test_observability_i3.py); `test_load_marker_is_registered_and_excluded` (test_observability_i3.py); `test_hot_path_uses_only_mocks` (test_observability_i3.py) |
| AC-147 | 1 | `test_standard_mcp_client_end_to_end` (test_e2e_i3.py) |
| AC-148 | 3 | `test_authorize_rejects_other_clients_redirect` (test_oauth_as.py); `test_authorize_rejects_path_traversal_redirect` (test_oauth_as.py); `test_loopback_redirect_with_other_port_is_accepted` (test_oauth_as.py) |
| AC-149 | 3 | `test_reauth_restores_connection_without_new_registration` (test_e2e_i3.py); `test_connect_endpoint_reconnects_from_hub_page` (test_oauth_as.py); `test_connect_requires_session_and_known_alias` (test_oauth_as.py) |

## 4. Покрытие по модулям

| Модуль | Строк | Не покрыто | Покрытие |
|---|---|---|---|
| `src/hub/__init__.py` | 5 | 0 | 100 % |
| `src/hub/app.py` | 137 | 6 | 96 % |
| `src/hub/auth.py` | 65 | 1 | 98 % |
| `src/hub/broker.py` | 372 | 45 | 88 % |
| `src/hub/catalog.py` | 381 | 9 | 98 % |
| `src/hub/cli.py` | 78 | 5 | 94 % |
| `src/hub/clock.py` | 40 | 7 | 82 % |
| `src/hub/crypto.py` | 69 | 8 | 88 % |
| `src/hub/db.py` | 193 | 6 | 97 % |
| `src/hub/errors.py` | 24 | 0 | 100 % |
| `src/hub/kv.py` | 141 | 38 | 73 % |
| `src/hub/litellm.py` | 67 | 3 | 96 % |
| `src/hub/logging_.py` | 45 | 2 | 96 % |
| `src/hub/login.py` | 254 | 13 | 95 % |
| `src/hub/metrics.py` | 107 | 1 | 99 % |
| `src/hub/middleware.py` | 54 | 0 | 100 % |
| `src/hub/migrate.py` | 49 | 5 | 90 % |
| `src/hub/migrations/env.py` | 23 | 8 | 65 % |
| `src/hub/migrations/versions/0001_i1_base.py` | 27 | 9 | 67 % |
| `src/hub/migrations/versions/0002_i3_oauth.py` | 47 | 19 | 60 % |
| `src/hub/oauth.py` | 301 | 20 | 93 % |
| `src/hub/oidc.py` | 110 | 14 | 87 % |
| `src/hub/permissions.py` | 95 | 9 | 91 % |
| `src/hub/proxy.py` | 267 | 19 | 93 % |
| `src/hub/routes/__init__.py` | 8 | 0 | 100 % |
| `src/hub/routes/admin.py` | 28 | 0 | 100 % |
| `src/hub/routes/api.py` | 103 | 8 | 92 % |
| `src/hub/routes/cli.py` | 57 | 1 | 98 % |
| `src/hub/routes/mcp.py` | 395 | 14 | 96 % |
| `src/hub/routes/oauth.py` | 350 | 25 | 93 % |
| `src/hub/routes/system.py` | 34 | 0 | 100 % |
| `src/hub/routes/web.py` | 191 | 18 | 91 % |
| `src/hub/settings.py` | 211 | 5 | 98 % |
| `src/hub/templating.py` | 18 | 0 | 100 % |
| `src/hub/web.py` | 48 | 5 | 90 % |
| `src/hub/websession.py` | 73 | 6 | 92 % |
| `src/hub/wellknown.py` | 34 | 0 | 100 % |
| **TOTAL** | **4501** | **329** | **93%** |

Осознанно не покрыто:

* `src/hub/kv.py` (73 %) — реализация `RedisKeyValueStore`: требует внешнего Redis, что запрещено
  правилом «только локальные моки» (R-N4). Протокол проверен на in-memory реализации (AC-141);
* `src/hub/migrations/versions/*.py` (60–67 %) — функции `downgrade()`: откат схемы не описан ни одним
  критерием приёмки I-3 (R-M1 требует только применения до `head`);
* `src/hub/migrations/env.py` (65 %) — ветка offline-режима Alembic (`run_migrations_offline`).

## 5. Баги

Багов не заведено: ни одно падение при разработке тестов не квалифицировано как `code_bug` —
все падения были ошибками самих тестов (`test_bug`) и исправлены:

| Падение | Классификация | Что сделано |
|---|---|---|
| `AC-98`: «испорченный» токен проходил проверку подписи | test_bug | правка последней буквы подписи меняла только незначащие биты base64url; заменено на сдвиг символа на 4 позиции алфавита (`tamper_signature`) |
| `AC-109`: второй `tools/list` не доходил до upstream | test_bug | ответ отдавался из кэша `tools/list` (ключ не зависит от пользователя); тест переведён на `tools/call` |
| `AC-112`, `AC-107`: refresh grant отвечал 401 `invalid_client` | test_bug | тестовый `client_id` не был зарегистрирован; `connected_client` теперь регистрирует клиента через `OAuthServer.register_client` |
| `AC-104`: `expires_at` из «сырого» SQL не имел `.timestamp()` | test_bug | добавлен `parse_db_datetime` |

## 6. Противоречия и неоднозначности критериев

Во всех случаях тестирование выполнено по **более конкретному правилу**; баг-репорты не заводились.

1. **AC-83 / AC-148 против R-O4.1 (решение 46).** В AC клиенты A и B различаются только портом
   (`http://127.0.0.1:19876/cb` и `http://127.0.0.1:20000/cb`), и от Hub ожидается 400. Но R-O4.1 прямо
   разрешает для loopback отличающийся порт при совпадении схемы, хоста и пути (RFC 8252). Тестируется
   по R-O4.1: чужой `redirect_uri` берётся с другим путём
   (`test_authorize_rejects_other_clients_redirect`), а разрешённое расхождение по порту зафиксировано
   отдельным тестом `test_loopback_redirect_with_other_port_is_accepted`. Обход `.../cb/../evil`
   отклоняется (`test_authorize_rejects_path_traversal_redirect`).
2. **AC-98, формулировка «изменённая последняя буква подписи».** В 43-символьной base64url-подписи
   HS256 два младших бита последнего символа незначимы, поэтому «соседняя» буква даёт тот же байтовый
   код подписи и токен остаётся валидным. Проверяемое поведение («неверная подпись → 401») сохранено:
   последняя буква сдвигается на 4 позиции алфавита, что гарантированно меняет байты подписи.
3. **AC-73 против R-C3 / AC-79.** AC-73 требует окружения «ровно из четырёх обязательных переменных
   I-1» и одновременно 200 на `/.well-known/oauth-protected-resource/mcp/gitlab`. Все серверы каталога
   репозитория имеют `status: beta`, и без переменных каталога (`${GITLAB_OAUTH_CLIENT_ID}` и др.)
   они переходят в состояние `unconfigured`, а PRM для них — 404 (R-C3, AC-79). Тест задаёт переменные
   **каталога** (они не являются настройками Hub) и оставляет ровно четыре обязательных `HUB_*`.
4. **AC-74 и CSRF-токен в HTML.** AC перечисляет для проверки HTML страниц `/ui/connections` и
   `/ui/servers/gitlab` — они CSRF-токен не содержат. Экран прав (`consent.html`) по R-W6
   (double-submit) содержит его в скрытом поле формы; это не нарушение AC-74 и проверяется отдельно
   (`test_consent_without_csrf_is_forbidden`).

## 7. Наблюдения по коду (не баги, AC не нарушены)

* `routes/oauth.py::_start_provider_oauth` перезаписывает `tx["step"]` значением `"provider"`, поэтому
  ветка `tx.get("step") == "scope_upgrade"` в `provider_callback` фактически недостижима. Флоу
  расширения прав на экране прав всё равно завершается корректно: после повторного OAuth целевой
  системы (со scope `api read_user`) при `HUB_CONSENT=always` экран прав показывается ещё раз, и
  подтверждение выдаёт код без нового обращения к AS системы. Зафиксировано тестом
  `test_consent_readwrite_repeats_provider_oauth`.
* Кэш `tools/list` (`toolscache:<alias>:<version>:<хеш прав>`) не зависит от пользователя — это
  соответствует решению 52 (ключ включает `catalog_version` и хеш прав), но означает, что одинаковые
  права разных пользователей делят один кэш. Учтено в тестах AC-109/AC-121.

## 8. После review-i3-1 и spec 2.1

Вход: `reports/review-i3-1.json` (2 must_fix с `assignee: test`) и ревизия 2.1 spec.md
(новые AC-150…AC-156; уточнены AC-73/74/83/98/141/145/148). Код продукта не изменялся
(`git status` по `src/` чист; временные мутации откатывались `git checkout -- src`).

### 8.1 Итоги прогона

| Показатель | Значение |
|---|---|
| Тестов в сьюте | **633** (было 612, добавлен 21) + 1 нагрузочный под маркером `load` |
| Прошло / упало | 633 / 0 (два последовательных прогона, разные seed `pytest-randomly`) |
| Skip / xfail | 0 |
| Deselected | 1 (`tests/test_load_sse.py::test_hundred_parallel_sse_streams`, маркер `load`) |
| `ruff check src tests` | чисто |
| `scripts/check_ac_traceability.py` | G6 OK: все **156** критериев покрыты |
| `scripts/check_zone_diff.py test` | ok: изменения в пределах зоны (`tests/`) |
| Покрытие `src/` (строки) | **93 %** |
| `diff-cover` к `main` | **92 %** (порог 90 %, exit 0) |
| Багов заведено | **0** (падений, квалифицированных как `code_bug`, не было) |
| Помечено `flaky_suspect` | 0 |

Прирост по файлам: `test_oauth_as.py` 66 → 73, `test_web.py` 27 → 31, `test_proxy.py` 28 → 31,
`test_settings_i3.py` 20 → 23, `test_migrations.py` 18 → 21, `test_permissions.py` 7 → 8.

### 8.2 Исправленные must_fix ревью (с доказательством мутациями)

**(a) AC-115 — клиентский `Authorization` не проверялся на удаление.** Причина, указанная ревью,
подтверждена: у всех facade тестового каталога `credential_headers` содержали `Authorization`, и
подставленное значение затирало проброшенный заголовок. Сделано:

* `tests/support.py::jira_facade` приведён к боевому `catalog.yaml`: `credential_headers` =
  `{X-Atlassian-Jira-Personal-Token: '{{access_token}}', X-Atlassian-Jira-Url: …}` — без `Authorization`;
* добавлен `tests/test_proxy.py::test_client_authorization_is_dropped_when_catalog_sets_other_header`:
  upstream `jira` получает токен целевой системы в своём заголовке, а клиентский `Authorization`
  (JWT Hub), `Cookie` и `X-Forwarded-For` отсутствуют; отдельно проверяется, что значение токена Hub
  не пришло **ни под каким именем заголовка** (перебор всех заголовков запроса).

Мутация `FORWARDED_HEADERS += ("authorization",)` (`src/hub/proxy.py:38`) → тест падает
(`assert 'Bearer eyJ…' is None`), остальные 28 тестов `test_proxy.py` проходят. Ранее эта мутация
давала 612 passed.

**(b) AC-131 — атрибуты cookie проверялись по склеенному `set-cookie`.** В `tests/support.py`
добавлены `set_cookie_header(response, name)` (выбор строки `Set-Cookie` конкретной cookie из
`headers.get_list`) и `cookie_attributes(raw)`. `test_oidc_login_creates_web_session` теперь проверяет
`HttpOnly` / `SameSite=Lax` / `Secure` / `Path` именно у `hub_session`, а у `hub_csrf` — отсутствие
`HttpOnly` (double-submit, R-W6) при `SameSite=Lax` и `Secure`.

Мутация `secure=False` только для `hub_session` (`src/hub/websession.py:114`) → тест падает
(`assert 'secure' in {'httponly': '', 'max-age': '28800', 'path': '/', 'samesite': 'lax'}`),
остальные 26 тестов `test_web.py` проходят. Ранее эта мутация давала 612 passed.

### 8.3 Новые критерии AC-150…AC-156

| AC | Тесты | Что зафиксировано | Проверка мутацией |
|---|---|---|---|
| AC-150 | `test_permissions.py::test_overlapping_group_masks_resolved_by_tool_name` | включённая `issues ['create_issue']` против выключенной `repo_write ['create_*']`: `tools/list` = `create_issue`, `list_mrs`; `tools/call create_issue` уходит на upstream; `create_merge_request` — JSON-RPC `-32001` c `data {tool, hint_url}` без обращения к upstream | `ToolFilter.allows` → `not _matches(tool, group_deny)` (поведение до R-P8 2.1): падает |
| AC-151 | `test_proxy.py::test_failed_half_open_probe_reopens_window` | провал пробы в half-open открывает окно немедленно: проба → 502 `-32004`, следующие два вызова → 503 `-32004` + `Retry-After` без обращения к upstream; после нового истечения окна успешная проба закрывает выключатель | `record_failure` со сбросом `failures = 0` при открытии окна: падает |
| AC-152 | `test_proxy.py::test_only_one_request_probes_upstream_in_half_open` | три параллельных запроса во время half-open: upstream удерживает пробу до сигнала теста, `hub.upstream.calls == 1`, два других — 503 `-32004` + `Retry-After`; после успеха `cb:gitlab == {failures: 0, open_until: 0.0}` | `set_if_absent(cb:<alias>:probe)` → безусловный `claimed = True`: падает («во время пробы на upstream ушёл не один запрос») |
| AC-153 | `test_oauth_as.py::test_forwarded_for_is_ignored_without_trust_proxy`, `…::test_trust_proxy_counts_limit_per_left_forwarded_address`, `…::test_forwarded_for_does_not_affect_auth_or_alias[False/True]` | дефолт `false` — заголовок игнорируется (третья регистрация 429 `rate_limited` + `Retry-After`); `true` — ключ по левому адресу `X-Forwarded-For` (по два запроса с `10.0.0.1` и `10.0.0.2` успешны, пятый с `10.0.0.1` — 429); заголовок не влияет на аутентификацию и выбор alias | `if True:` → падает «off»-тест; `if False:` → падает «on»-тест |
| AC-154 | `test_web.py::test_id_token_with_forbidden_alg_is_rejected[jwks_ok/jwks_down × HS256/none]`, `…::test_oidc_login_creates_web_session` | `id_token` с `alg=HS256` (HMAC по значению открытого ключа из JWKS) и `alg=none` → 400, cookie нет, строки в `sessions` нет; к JWKS не было ни одного запроса (`MockOIDC.jwks_requests == []`), результат одинаков при доступном и недоступном JWKS; RS256 по-прежнему создаёт сессию | `ALLOWED_ALGORITHMS += ['HS256', 'none']`: падают все 4 параметра |
| AC-155 | `test_oauth_as.py::test_token_requires_redirect_uri_used_for_code` | обмен кода без `redirect_uri` → 400 `invalid_grant` с русским описанием, токены не выданы, `refresh_tokens` пуст; повтор с корректным `redirect_uri` → 200 (код после отклонения остался неиспользованным) | — (проверка соответствует ветке R-O8, добавленной dev'ом) |
| AC-156 | `test_settings_i3.py::test_start_without_redis_url_warns_about_unshared_state`, `…::test_start_with_redis_url_does_not_warn`, `test_migrations.py::test_postgres_migrations_take_advisory_lock`, `…::test_sqlite_migrations_do_not_take_lock`, `…::test_lock_is_taken_before_revisions_are_applied` | пустой `HUB_REDIS_URL` → ровно одна запись WARNING `kv_in_memory` с упоминанием `HUB_REDIS_URL`, denylist, MCP-сессий, окон rate-limit и circuit-breaker; приложение обслуживает `/health` и `/api/catalog`; при заданном `HUB_REDIS_URL` записи нет (`app.state.kv` — `RedisKeyValueStore`); на диалекте `postgresql` выполняется ровно один statement `pg_advisory_xact_lock` с параметром `ADVISORY_LOCK_ID`, на `sqlite` — ни одного; блокировка берётся до применения ревизий (в момент вызова `alembic_version` ещё нет), в SQL прогона на SQLite нет `pg_advisory` | — |

PostgreSQL в тестах не поднимался: advisory-блокировка проверяется на границе БД — мок соединения
SQLAlchemy (`_RecordingConnection` с `dialect.name`) плюс перехват реального SQL прогона миграций на
SQLite через событие `before_cursor_execute`.

### 8.4 Уточнённые критерии (AC-73/74/83/98/141/145/148)

* **AC-83** — противоречие снято ревизией 2.1. `test_loopback_redirect_with_other_port_is_accepted`
  перемаркирован на AC-83 и усилен: расхождение только по порту loopback не 400, флоу доходит до
  выдачи кода (редирект на предъявленный `http://127.0.0.1:20000/cb`), а код **привязан к
  предъявленному** `redirect_uri` — обмен с ним 200, обмен с зарегистрированным `…:19876/cb` →
  `invalid_grant`.
* **AC-148** — три негативных теста без изменений (чужой путь, `…/cb/../evil`, `https://evil.test/cb`);
  комментарий к `CLIENT_B_REDIRECT` приведён к формулировке 2.1 (клиент B отличается **путём**).
* **AC-98** — `tamper_signature(token, position)` принимает позицию символа подписи; тест
  параметризован по `first / middle / last` и дополнительно проверяет, что декодированные **байты**
  подписи отличаются (`signature_bytes`).
* **AC-141** — вместо `mcpsess:*` проверяется `mcpsess:gitlab:<id>` (alias в ключе, ровно три сегмента)
  и отсутствие записей по ключу `mcpsessn:*` (отдельного счётчика сессий в таблице ключей больше нет);
  TTL 86400 проверяется на том же ключе.
* **AC-145** — `HUB_TRUST_PROXY` добавлен в `I3_SETTINGS_VARS`; `deploy/.env.example` содержит строку.
* **AC-73** — добавлен `test_catalog_vars_are_not_hub_settings`: без переменных **каталога** и без
  `KEYCLOAK_*` (проверяется, что в окружении нет ни одной `HUB_*`/`KEYCLOAK_*`) Hub поднимается,
  `/health`, `/.well-known/opencode` и метаданные AS — 200, а PRM сервера — 404 (R-C3/AC-79).
* **AC-74** — к проверке «CSRF-токена нет в логах, аудите, `/metrics` и HTML `/ui/*`» добавлена
  положительная: на экране прав (`/oauth/authorize`) токен присутствует скрытым полем формы
  (`hidden_inputs(consent_page.text)[CSRF_FIELD] == csrf_token`) — требование R-W6.

Пункты 1–4 раздела 6 (противоречия) закрыты ревизией 2.1 spec.md и в тестах больше не помечаются
как расхождения.

### 8.5 Покрытие затронутых модулей

| Модуль | Покрытие |
|---|---|
| `src/hub/permissions.py` | 90 % |
| `src/hub/proxy.py` | 93 % |
| `src/hub/oidc.py` | 87 % |
| `src/hub/websession.py` | 92 % |
| `src/hub/migrate.py` | 91 % |
| `src/hub/routes/oauth.py` | 93 % |
| `src/hub/routes/web.py` | 91 % |
| `src/hub/app.py` | 96 % |
