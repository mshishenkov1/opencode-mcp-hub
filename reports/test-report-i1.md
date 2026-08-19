# Отчёт TEST-AGENT — итерация I-1 (Hub: вход по SSO, ключ LiteLLM, каталог, well-known)

Ветка: `pipeline/i1-hub-login-catalog-20260818`. Дата: 2026-08-19 (обновлено после ревизии спецификации 1.1,
коммит `5472fa3`: уточнены R-K3/R-C1/R-C2/R-A5/R-L9 и AC-09/AC-47/AC-59 — тесты приведены к новым формулировкам).
Входы: `src/hub/`, `spec.md` (ревизия 1.1), `acceptance-criteria.yaml` (69 AC), `catalog.yaml`, `pyproject.toml`, `pipeline.config.yaml`.
Все проверки — против локальных моков: LiteLLM через `respx.MockRouter` + `httpx.MockTransport`
(без сети), SQLite `:memory:`/временный файл, in-memory KeyValueStore, `hub.clock.ManualClock`,
временные YAML-каталоги. Обращений к внешним системам нет.

## 1. Итог прогона

| Показатель | Значение |
|---|---|
| Команда | `.venv/bin/python -m pytest -q -rs --tb=short` (`commands.test`) |
| Собрано тестов | **300** (parametrize развёрнут; 190 функций `test_*` в 7 файлах) |
| Прошло | **300** |
| Упало | **0** |
| xfail | **0** (xfail-тест `test_wellknown_body_has_no_env_prefix_literal` удалён после ревизии 1.1 — заменён тестами по новой формулировке AC-59, см. §5) |
| skip | 0 (тест `test_installed_entrypoint_mcp_hub_help` пропускается только при отсутствии console-script `mcp-hub` в venv; сейчас выполняется) |
| flaky_suspect | 0 (сьют прогнан 3 раза подряд — стабильно зелёный, время везде подменено `ManualClock`) |
| Баги (`bugs/BUG-*.json`) | **0** — нарушений AC не выявлено |
| Трассировка AC (G6) | `scripts/check_ac_traceability.py` → **G6 OK: все 69 критериев покрыты** |
| Линт тестов | `ruff check src tests` → All checks passed |

## 2. Покрытие (`commands.coverage`)

`.venv/bin/python -m pytest -q --cov=src --cov-report=xml:reports/coverage.xml --cov-report=term`

```
src/hub/__init__.py              5      0   100%
src/hub/app.py                  92      5    95%   62-66, 74, 125
src/hub/auth.py                 50      0   100%
src/hub/catalog.py             364     14    96%   49-50, 58, 61, 70, 73, 81, 84, 89, 95, 100, 237, 293, 312
src/hub/cli.py                  48      2    96%   83-84
src/hub/clock.py                40      7    82%   41, 44, 64-69
src/hub/db.py                  107      8    93%   26, 32, 38, 121, 123, 175-177
src/hub/errors.py               24      0   100%
src/hub/kv.py                   90     29    68%   89-91, 94-97, 100-107, 110, 113-116, 119-127, 130, 135
src/hub/litellm.py              67      3    96%   28, 121-122
src/hub/logging_.py             44      3    93%   49-50, 52
src/hub/login.py               256     13    95%   108-109, 115-116, 377-396
src/hub/metrics.py              58      1    98%   18
src/hub/middleware.py           53      0   100%
src/hub/routes/__init__.py       5      0   100%
src/hub/routes/admin.py         26      0   100%
src/hub/routes/api.py           47      1    98%   34
src/hub/routes/cli.py           57      1    98%   36
src/hub/routes/system.py        34      0   100%
src/hub/settings.py            130      3    98%   41-42, 204
src/hub/wellknown.py            34      0   100%
TOTAL                         1631     90    94%
```

* **TOTAL 94 %** (1631 stmt, 90 miss). `diff-cover reports/coverage.xml --compare-branch=main --fail-under=90` → **94 %** ≥ 90 % (гейт G2 проходит).
* Непокрытое — в основном `RedisKeyValueStore` (`kv.py`, требует Redis; тесты только против in-memory),
  `SystemClock`/`ManualClock.set` (`clock.py`), `cmd_serve` реальный запуск uvicorn (замокан), тривиальные `repr`/`__eq__`.
* Замечание по измерению: строки `login.py:377-396`, `db.py:175-177`, `routes/api.py:34` фактически исполняются
  (данные в БД проверяются тестами), но трассировщик coverage по умолчанию (`ctrace`) теряет их после переключения
  greenlet внутри SQLAlchemy-async. С `COVERAGE_CORE=sysmon` (Python 3.12) те же тесты дают **95 %** и эти строки
  покрыты. Рекомендация DEV/оркестратору (вне зоны test-agent): добавить в `pyproject.toml`
  `[tool.coverage.run] concurrency = ["greenlet", "thread"]` либо задать `COVERAGE_CORE=sysmon` в CI.

## 3. Структура тестов

| Файл | Тестов (функций) | Область |
|---|---|---|
| `tests/conftest.py` | — | фикстуры: `make_hub`/`hub` (create_app + `asgi-lifespan` + httpx `ASGITransport`), `litellm` (respx-роутер), `clock` (`ManualClock`), `catalog_path`, автоочистка `HUB_*` из окружения, захват логов `hub` |
| `tests/support.py` | — | конструкторы каталогов (`native_server`, `facade_server`, `catalog_doc`, `write_catalog`), моки LiteLLM (`mock_start`, `mock_poll`, `mock_key_generate`, `teams_body`, `ready_body`), `make_jwt`, прямой доступ к БД по схеме §6 (`insert_user/insert_key/insert_connection`, `fetch_rows`, `audit_rows`, `dump_all_tables`) |
| `tests/test_settings.py` | 17 | AC-01..AC-06 (R-K1..R-K4) |
| `tests/test_catalog.py` | 54 | AC-07..AC-20, AC-22, AC-23 (R-C1..R-C4, R-C6) |
| `tests/test_login.py` | 55 | AC-24..AC-47 (R-L1..R-L10) |
| `tests/test_api.py` | 29 | AC-48..AC-57, AC-61..AC-64 (R-L6, R-A1..R-A4, R-A6, R-A7, R-S4) |
| `tests/test_wellknown.py` | 11 | AC-58..AC-60 (R-A5, R-A8) |
| `tests/test_storage.py` | 11 | AC-65..AC-68 (R-S1..R-S4) |
| `tests/test_cli.py` | 13 | AC-21, AC-69 (R-C5, R-S5) |

Каждый тест привязан маркером `@pytest.mark.ac("AC-NN")`; негативные и граничные сценарии из AC покрыты
(неверные/отсутствующие секреты, истечение TTL, границы окна rate-limit 59/61 с, дросселирование 1.99/2.0 с,
кэш аутентификации 59/61 с, alias 1/32/33 символа, `client` 128/129 символов, `X-Request-ID` 128/129 символов,
5xx/сеть/4xx/невалидные тела LiteLLM по всем трём маршрутам, невалидный/удалённый файл при reload и т.д.).

## 4. Таблица AC → тесты

| AC | Тестов | Тесты (файл::функция) |
|---|---|---|
| AC-01 | 4 | `test_settings.py::test_missing_required_env_var_fails_start_with_name`<br>`test_settings.py::test_all_required_env_vars_present_app_starts`<br>`test_settings.py::test_invalid_numeric_or_level_settings_fail_with_var_name`<br>`test_settings.py::test_empty_required_values_fail_with_var_name` |
| AC-02 | 3 | `test_settings.py::test_invalid_fernet_key_rejected`<br>`test_settings.py::test_valid_fernet_key_accepted`<br>`test_settings.py::test_fernet_key_with_invalid_base64_chars_rejected` |
| AC-03 | 2 | `test_settings.py::test_defaults_visible_in_wellknown`<br>`test_settings.py::test_default_setting_values` |
| AC-04 | 4 | `test_settings.py::test_auth_command_default_with_public_url_substitution`<br>`test_settings.py::test_auth_command_custom_json_array_with_placeholder`<br>`test_settings.py::test_auth_command_invalid_json_fails_start`<br>`test_settings.py::test_urls_normalized_without_trailing_slash` |
| AC-05 | 1 | `test_settings.py::test_secrets_never_logged` |
| AC-06 | 3 | `test_settings.py::test_create_app_with_settings_object_without_env`<br>`test_settings.py::test_settings_object_missing_required_field_fails`<br>`test_settings.py::test_litellm_client_injectable_via_app_state` |
| AC-07 | 2 | `test_catalog.py::test_repo_catalog_loads_at_start`<br>`test_catalog.py::test_repo_catalog_without_vars_hides_beta_servers` |
| AC-08 | 8 | `test_catalog.py::test_missing_required_field_reports_path`<br>`test_catalog.py::test_missing_other_required_fields`<br>`test_catalog.py::test_empty_required_string_rejected`<br>`test_catalog.py::test_unknown_field_rejected_strict_schema`<br>`test_catalog.py::test_nested_auth_field_missing_reports_nested_path`<br>`test_catalog.py::test_top_level_schema_errors`<br>`test_catalog.py::test_more_top_level_schema_errors`<br>`test_catalog.py::test_unreadable_catalog_path_is_error` |
| AC-09 | 4 | `test_catalog.py::test_invalid_alias_reports_path` (`Bad_Alias`, `-x`, `1abc`, `ABC`, `with space`, 33 символа, `-abc`, пустой, `a_b`, `a-B`)<br>`test_catalog.py::test_duplicate_alias_reports_alias`<br>`test_catalog.py::test_valid_aliases_accepted` (`a`, 32 символа, `ab`, `gitlab-platform2`, `a-`, `a0`, `z`+31×`-`)<br>`test_catalog.py::test_single_char_alias_visible_in_catalog_and_wellknown` |
| AC-10 | 4 | `test_catalog.py::test_native_without_mcp_url_invalid`<br>`test_catalog.py::test_facade_without_required_field_invalid`<br>`test_catalog.py::test_facade_with_empty_credential_headers_invalid`<br>`test_catalog.py::test_facade_auth_field_type_errors` |
| AC-11 | 5 | `test_catalog.py::test_invalid_enum_values_rejected`<br>`test_catalog.py::test_invalid_permission_group_preset_rejected`<br>`test_catalog.py::test_duplicate_group_ids_rejected`<br>`test_catalog.py::test_permission_kinds_consent_and_tool_filter_accepted`<br>`test_catalog.py::test_permission_model_without_kind_reports_kind_path` |
| AC-12 | 5 | `test_catalog.py::test_var_substituted_from_environment`<br>`test_catalog.py::test_missing_var_for_ga_server_fails_with_name_and_path`<br>`test_catalog.py::test_missing_var_for_deprecated_server_fails`<br>`test_catalog.py::test_multiple_vars_in_one_string_and_nested`<br>`test_catalog.py::test_missing_var_in_defaults_fails` |
| AC-13 | 3 | `test_catalog.py::test_repo_catalog_without_vars_hides_beta_servers`<br>`test_catalog.py::test_beta_server_with_missing_var_is_unconfigured_and_hidden`<br>`test_catalog.py::test_beta_server_with_var_present_is_visible` |
| AC-14 | 2 | `test_catalog.py::test_env_ref_not_required_and_never_serialized`<br>`test_catalog.py::test_env_ref_value_not_in_repr` |
| AC-15 | 3 | `test_catalog.py::test_env_ref_in_disallowed_field_is_schema_error`<br>`test_catalog.py::test_env_ref_in_other_disallowed_fields`<br>`test_catalog.py::test_env_ref_allowed_in_secret_fields` |
| AC-16 | 3 | `test_catalog.py::test_ref_resolved_one_level`<br>`test_catalog.py::test_ref_target_with_var_is_substituted`<br>`test_catalog.py::test_ref_nested_inside_field_value` |
| AC-17 | 4 | `test_catalog.py::test_ref_to_unknown_alias_fails_with_path_and_ref`<br>`test_catalog.py::test_ref_to_field_that_is_itself_ref_fails`<br>`test_catalog.py::test_ref_bad_format_or_unknown_field_fails`<br>`test_catalog.py::test_ref_object_with_extra_keys_or_non_string_fails` |
| AC-18 | 3 | `test_catalog.py::test_reload_disabled_without_admin_token`<br>`test_catalog.py::test_reload_forbidden_with_wrong_or_missing_token`<br>`test_catalog.py::test_reload_empty_admin_token_setting_disables_endpoint` |
| AC-19 | 1 | `test_catalog.py::test_reload_replaces_catalog_and_audits` |
| AC-20 | 3 | `test_catalog.py::test_reload_invalid_file_keeps_old_catalog`<br>`test_catalog.py::test_reload_deleted_file_keeps_old_catalog`<br>`test_catalog.py::test_reload_broken_yaml_keeps_old_catalog` |
| AC-21 | 7 | `test_cli.py::test_catalog_validate_valid_returns_0`<br>`test_cli.py::test_catalog_validate_invalid_returns_1_with_field_path`<br>`test_cli.py::test_catalog_validate_missing_file_returns_1`<br>`test_cli.py::test_catalog_validate_reports_unconfigured_beta_servers`<br>`test_cli.py::test_catalog_validate_uses_hub_catalog_path_env_by_default`<br>`test_cli.py::test_catalog_validate_defaults_to_local_catalog_yaml`<br>`test_cli.py::test_catalog_validate_needs_no_other_hub_env` |
| AC-22 | 3 | `test_catalog.py::test_public_view_has_allowed_fields_only`<br>`test_catalog.py::test_public_permission_model_shapes`<br>`test_catalog.py::test_public_view_optional_contact_docs_null` |
| AC-23 | 2 | `test_catalog.py::test_empty_catalog_valid`<br>`test_catalog.py::test_catalog_with_defaults_and_null_servers_valid` |
| AC-24 | 2 | `test_login.py::test_cli_start_creates_hub_session`<br>`test_login.py::test_cli_start_two_sessions_have_distinct_ids_and_secrets` |
| AC-25 | 4 | `test_login.py::test_expires_in_is_min_of_hub_ttl_and_litellm`<br>`test_login.py::test_expires_in_defaults_to_hub_ttl_when_litellm_omits_it`<br>`test_login.py::test_expires_in_uses_hub_ttl_when_smaller`<br>`test_login.py::test_session_expires_after_min_ttl` |
| AC-26 | 1 | `test_login.py::test_cli_start_litellm_unavailable` |
| AC-27 | 2 | `test_login.py::test_cli_start_invalid_body_rejected`<br>`test_login.py::test_cli_start_empty_body_and_boundary_client_accepted` |
| AC-28 | 3 | `test_login.py::test_cli_start_rate_limit_sliding_window`<br>`test_login.py::test_cli_start_rate_limit_retry_after_reflects_window`<br>`test_login.py::test_cli_start_rate_limit_is_per_ip` |
| AC-29 | 4 | `test_login.py::test_poll_unknown_login_id_is_404`<br>`test_login.py::test_poll_expired_session_is_404`<br>`test_login.py::test_poll_just_before_ttl_still_alive`<br>`test_login.py::test_session_ttl_not_extended_by_client_activity` |
| AC-30 | 2 | `test_login.py::test_poll_wrong_or_missing_secret_is_403`<br>`test_login.py::test_poll_secret_check_precedes_upstream_and_team_state` |
| AC-31 | 1 | `test_login.py::test_poll_pending_forwards_litellm_secret_header` |
| AC-32 | 3 | `test_login.py::test_poll_throttled_to_one_upstream_call_per_2s`<br>`test_login.py::test_poll_throttle_boundary_exactly_2s`<br>`test_login.py::test_poll_throttle_returns_cached_response_code_and_body` |
| AC-33 | 3 | `test_login.py::test_multiple_teams_require_selection_and_stop_polling`<br>`test_login.py::test_teams_without_details_use_id_as_alias`<br>`test_login.py::test_teams_as_list_of_objects_without_details` |
| AC-34 | 1 | `test_login.py::test_choose_team_from_list_is_forwarded` |
| AC-35 | 1 | `test_login.py::test_choose_team_outside_list_or_invalid_body_rejected` |
| AC-36 | 2 | `test_login.py::test_choose_team_access_and_state_errors`<br>`test_login.py::test_choose_team_on_fresh_session_is_409` |
| AC-37 | 3 | `test_login.py::test_single_team_selected_automatically`<br>`test_login.py::test_single_team_from_teams_list_without_details`<br>`test_login.py::test_single_team_loop_is_invalid_response` |
| AC-38 | 3 | `test_login.py::test_empty_team_list_is_invalid_response_but_session_survives`<br>`test_login.py::test_unexpected_poll_body_is_invalid_response`<br>`test_login.py::test_ready_without_user_id_anywhere_is_invalid_response` |
| AC-39 | 1 | `test_login.py::test_user_without_teams_gets_key_without_team_id` |
| AC-40 | 5 | `test_login.py::test_ready_creates_persistent_key`<br>`test_login.py::test_key_alias_prefix_configurable_and_no_client`<br>`test_login.py::test_user_id_and_email_derivation`<br>`test_login.py::test_non_jwt_key_uses_upstream_user_id_and_null_email`<br>`test_login.py::test_jwt_with_non_object_payload_is_tolerated` |
| AC-41 | 4 | `test_login.py::test_key_generate_4xx_falls_back_to_jwt`<br>`test_login.py::test_jwt_fallback_without_exp_has_null_expires_in`<br>`test_login.py::test_jwt_fallback_expired_token_gives_zero`<br>`test_login.py::test_jwt_key_authenticates_api` |
| AC-42 | 2 | `test_login.py::test_key_generate_5xx_returns_502_and_retries_only_key_generate`<br>`test_login.py::test_key_generate_retry_is_throttled` |
| AC-43 | 1 | `test_login.py::test_login_session_is_single_use` |
| AC-44 | 1 | `test_login.py::test_key_and_user_persisted_hashed_with_audit` |
| AC-45 | 1 | `test_login.py::test_repeated_login_adds_key_and_keeps_old_valid` |
| AC-46 | 3 | `test_login.py::test_poll_5xx_or_network_is_502_and_session_survives`<br>`test_login.py::test_poll_4xx_removes_session`<br>`test_login.py::test_poll_errors_two_sessions_side_by_side` |
| AC-47 | 2 | `test_login.py::test_cli_responses_do_not_leak_litellm_secret_or_login_id` (`ll-secret` нигде; `browser_url == <LITELLM>/sso/key/generate?source=litellm-cli&key=ll-1`; после удаления `browser_url` из ответа `/cli/start` `ll-1` нет ни в одном теле `/cli/*`)<br>`test_login.py::test_cli_error_and_expired_responses_do_not_leak_litellm_ids` (502/404 тоже без `ll-1`/`ll-secret`) |
| AC-48 | 4 | `test_api.py::test_bearer_auth_valid_key_passes`<br>`test_api.py::test_bearer_auth_rejects_missing_or_bad`<br>`test_api.py::test_all_bearer_endpoints_require_auth`<br>`test_api.py::test_bearer_scheme_case_insensitive` |
| AC-49 | 2 | `test_api.py::test_x_litellm_api_key_header_accepted`<br>`test_api.py::test_authorization_takes_precedence_over_x_litellm_api_key` |
| AC-50 | 2 | `test_api.py::test_auth_result_cached_for_60s`<br>`test_api.py::test_negative_auth_result_not_cached` |
| AC-51 | 3 | `test_api.py::test_health_and_ready`<br>`test_api.py::test_ready_503_when_db_disposed`<br>`test_api.py::test_ready_503_when_db_engine_broken` |
| AC-52 | 1 | `test_api.py::test_api_me_reflects_key_used` |
| AC-53 | 2 | `test_api.py::test_api_catalog_requires_auth_and_has_connection_block`<br>`test_api.py::test_api_catalog_preserves_file_order` |
| AC-54 | 1 | `test_api.py::test_include_deprecated_filter` |
| AC-55 | 2 | `test_api.py::test_audience_filter`<br>`test_api.py::test_audience_intersection_with_user_groups` |
| AC-56 | 1 | `test_api.py::test_me_connections` |
| AC-57 | 2 | `test_api.py::test_catalog_connection_block_reflects_rows`<br>`test_api.py::test_catalog_connection_block_is_per_user` |
| AC-58 | 3 | `test_wellknown.py::test_wellknown_auth_provider_and_remote_config`<br>`test_wellknown.py::test_wellknown_reflects_custom_provider_settings`<br>`test_wellknown.py::test_wellknown_needs_no_auth_and_ignores_bad_bearer` |
| AC-59 | 4 | `test_wellknown.py::test_wellknown_mcp_entries_for_visible_servers_without_secrets` (каталог по AC-59 1.1: `client_secret env:GL_SECRET`, `credential_headers` `env:GL_TOKEN`, `static_headers` `env:GL_STATIC`; в теле нет `upstream`/`client_secret`/`credential_headers`/`static_headers`/`GL_SECRET`/`GL_TOKEN`/`GL_STATIC`/значений; в `config.mcp` нет `env:`)<br>`test_wellknown.py::test_wellknown_env_prefix_only_in_opencode_placeholders` (ровно 2 плейсхолдера `{env:MAGNIT_COPILOT_KEY}`; после их удаления `env:` в теле нет)<br>`test_wellknown.py::test_wellknown_env_prefix_check_follows_custom_env_name` (то же при `HUB_WELLKNOWN_ENV_NAME=CORP_KEY`)<br>`test_wellknown.py::test_wellknown_mcp_uses_public_url_and_deprecated_included` |
| AC-60 | 4 | `test_wellknown.py::test_wellknown_etag_304_and_changes_after_reload`<br>`test_wellknown.py::test_wellknown_etag_stable_and_mismatch_returns_200`<br>`test_wellknown.py::test_wellknown_etag_differs_between_settings`<br>`test_wellknown.py::test_wellknown_if_none_match_star_and_weak` |
| AC-61 | 1 | `test_api.py::test_remote_config_requires_bearer_and_is_empty_by_default` |
| AC-62 | 1 | `test_api.py::test_remote_config_includes_connected_servers_only` |
| AC-63 | 3 | `test_api.py::test_uniform_error_format_and_nosniff`<br>`test_api.py::test_error_codes_are_snake_case_and_have_no_status_outside_cli`<br>`test_api.py::test_metrics_and_wellknown_have_nosniff_too` |
| AC-64 | 4 | `test_api.py::test_request_id_preserved_and_logged`<br>`test_api.py::test_request_id_generated_when_missing`<br>`test_api.py::test_request_id_too_long_is_replaced`<br>`test_api.py::test_request_id_present_on_error_responses` |
| AC-65 | 3 | `test_storage.py::test_schema_created_at_startup_in_sqlite_file`<br>`test_storage.py::test_unique_constraints_enforced`<br>`test_storage.py::test_schema_creation_is_idempotent_across_restarts` |
| AC-66 | 3 | `test_storage.py::test_inmemory_kv_respects_ttl`<br>`test_storage.py::test_inmemory_kv_ttl_boundary_and_overwrite`<br>`test_storage.py::test_inmemory_kv_returns_copies` |
| AC-67 | 3 | `test_storage.py::test_metrics_exposes_requests_latency_and_sessions`<br>`test_storage.py::test_metrics_path_label_is_route_template`<br>`test_storage.py::test_metrics_active_sessions_gauge_tracks_lifecycle` |
| AC-68 | 2 | `test_storage.py::test_audit_log_has_no_secrets`<br>`test_storage.py::test_audit_details_are_json_objects` |
| AC-69 | 6 | `test_cli.py::test_cli_help_lists_serve_and_catalog`<br>`test_cli.py::test_cli_catalog_help_lists_validate_and_path`<br>`test_cli.py::test_cli_serve_help_has_host_and_port`<br>`test_cli.py::test_cli_serve_passes_host_and_port_to_uvicorn`<br>`test_cli.py::test_cli_without_command_or_unknown_command_is_error`<br>`test_cli.py::test_installed_entrypoint_mcp_hub_help` |

## 5. Противоречия спецификации — разрешены ревизией 1.1

Противоречия 1–3, зафиксированные в первой версии отчёта, устранены ревизией спецификации 1.1
(`spec.md`, блок «Ревизия 1.1», коммит `5472fa3`); тесты приведены к новым формулировкам, временных
обходов (`xfail`) в сьюте не осталось.

1. **AC-59 ↔ AC-58 / R-A5 (подстрока `env:`) — разрешено.** Ревизия 1.1: плейсхолдеры OpenCode
   `{env:<HUB_WELLKNOWN_ENV_NAME>}` в `provider.*.options.apiKey` и `remote_config.headers.Authorization` разрешены;
   запрет касается ссылок каталога `env:VAR`, имён таких переменных и значений секретов (R-K3, R-C2, R-A5, AC-59).
   Xfail-тест `test_wellknown_body_has_no_env_prefix_literal` удалён; вместо него —
   `test_wellknown_env_prefix_only_in_opencode_placeholders` (после удаления всех `{env:MAGNIT_COPILOT_KEY}` подстроки
   `env:` нет; нет `upstream`, `client_secret`, `credential_headers`, `static_headers`, `GL_SECRET`, `GL_TOKEN`,
   `GL_STATIC`) и `test_wellknown_env_prefix_check_follows_custom_env_name` (то же при другом `HUB_WELLKNOWN_ENV_NAME`);
   каталог основного теста дополнен `env:`-ссылками в `credential_headers` и `static_headers` по новому given AC-59.
   Реализация удовлетворяет новой формулировке без изменений — все тесты зелёные.
2. **R-C1 регэксп alias ↔ AC-54/AC-55 — разрешено.** Ревизия 1.1: `^[a-z][a-z0-9-]{0,31}$` (1–32 символа).
   AC-09 покрыт по новому перечню: невалидные `Bad_Alias`, `-x`, `1abc`, `ABC`, `with space`, 33 символа
   (плюс `-abc`, пустой, `a_b`, `a-B`) → ошибка с путём `servers[0].alias`; контрольные валидные `a` и 32 символа
   (плюс `ab`, `gitlab-platform2`, `a-`, `a0`, `z`+31×`-`) → приложение создаётся; alias `a` дополнительно проходит
   путь до `/api/catalog` и well-known (`test_single_char_alias_visible_in_catalog_and_wellknown`).
3. **R-L9 ↔ R-L1 (`login_id` LiteLLM) — разрешено.** Ревизия 1.1: `login_id` LiteLLM допустим только как значение
   `key=` внутри `browser_url` ответа `/cli/start`; `poll_secret` LiteLLM — нигде. AC-47 проверяется буквально:
   `browser_url == '<LITELLM>/sso/key/generate?source=litellm-cli&key=ll-1'`, после удаления значения `browser_url`
   из ответа `/cli/start` подстроки `ll-1` нет ни в одном теле `/cli/*` (pending, team_selection_required, 400/403/404
   при выборе команды, ready, 404 после ready, 404/403 неверные id/секрет); дополнительно — ответы 502
   `litellm_unavailable` и 404 `login_expired` (`test_cli_error_and_expired_responses_do_not_leak_litellm_ids`).
4. **`catalog.yaml` репозитория изменился в ходе итерации** (все серверы теперь `status: beta`, коммит
   `5bc1139`): AC-07 («переменные `${…}` заданы») проверяется на копии файла с заданными переменными
   (`test_repo_catalog_loads_at_start`, 5 серверов видны); дополнительно проверено, что без переменных
   приложение стартует и beta-серверы скрыты (`test_repo_catalog_without_vars_hides_beta_servers`, AC-07/AC-13).
   Сценарий «ga-сервер без `${VAR}` → ошибка старта» покрыт синтетическими каталогами (AC-12). Не противоречие
   спецификации — замечание о входных данных.
5. Неоднозначность (без последствий для тестов, ревизией 1.1 не затронута): `{"client": null}` в `POST /cli/start` —
   спека допускает `client?: string`; поведение для `null` не задано, в тестах не фиксируется.

## 6. Баги

Не обнаружено (в том числе после приведения тестов к ревизии 1.1). Каталог `bugs/` пуст. Все 69 AC имеют минимум
один проходящий тест; xfail/skip — 0.

## 7. Флаки

Не отмечено (`@pytest.mark.flaky_suspect` не использовался). Источники нестабильности исключены конструктивно:
время — `ManualClock`, БД — отдельный `:memory:` на приложение, HTTP — `MockTransport` без сети,
окружение `HUB_*` очищается autouse-фикстурой.
