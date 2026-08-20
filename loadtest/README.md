# Нагрузочная проверка Hub (D6-08…D6-10)

Изолированный контур: Hub, Postgres, Redis и **мок** upstream MCP. Ни один сценарий
не обращается в боевые системы — это проверяется тремя независимыми способами
(см. «Защита от боевых адресов»).

| Файл | Что это |
|---|---|
| `docker-compose.loadtest.yml` | контур: `hub`, `postgres`, `redis`, `mock-upstream` |
| `catalog.loadtest.yaml` | каталог, где все upstream и OAuth-эндпоинты — мок |
| `mock_upstream/` | мок MCP-сервера и токен-эндпоинта целевой системы (FastAPI) |
| `k6/wellknown.js` | сценарий (а): «холодный старт» 5 000 клиентов |
| `k6/mcp.js` | сценарий (б): MCP-трафик 30 000 виртуальных сессий, 3–5 % активны |
| `k6/refresh_storm.js` | сценарий (в): 500 одновременных `refresh_token` |
| `k6/lib/` | общие настройки, пороги, помощники MCP |
| `tools/seed.py` | сеет пользователей, ключи, подключения и токены |
| `tools/check_no_prod.sh` | грепом проверяет, что в контуре нет боевых адресов |
| `tools/overhead.py` | считает добавку proxy по итогу прогона k6 |

## Пороги (спека S-01/S-02)

| Метрика | Порог | Где проверяется |
|---|---|---|
| Добавка proxy к времени ответа upstream, p50 | ≤ 15 мс | `mcp_via_hub` − `mcp_direct`, `tools/overhead.py` |
| То же, p95 | ≤ 50 мс | там же |
| `/api/*` и `/remote-config`, p95 | ≤ 100 мс | пороги `remote_config`, `wellknown_*` |
| Доля ошибок | < 0,1 % | `http_req_failed`, `scenario_errors` |

Отдельного порога на `/oauth/token` спека не задаёт: по умолчанию берётся порог
`/api/*` как ориентир, согласованное значение — `THRESHOLD_REFRESH_P95`.

Мок отвечает без искусственной задержки (`MOCK_LATENCY_MS=0`), поэтому время ответа
через Hub — это почти целиком добавка Hub. Точная разница считается вычитанием
базового сценария (`mcp_direct`), который гоняет тот же профиль запросов прямо в мок.

## Как прогнать

```bash
# 1. Поднять контур (образ Hub собирается из deploy/Dockerfile.hub)
docker compose -f loadtest/docker-compose.loadtest.yml -p hubload up -d --build

# 2. Убедиться, что в контуре нет боевых адресов
bash loadtest/tools/check_no_prod.sh

# 3. Засеять данные (нужен asyncpg: pip install -e ".[postgres]")
export HUB_PUBLIC_URL=http://localhost:8000
export HUB_DATABASE_URL=postgresql+asyncpg://hub:hub@localhost:55432/hub
export HUB_REDIS_URL=redis://localhost:56379/0
export HUB_CATALOG_PATH=loadtest/catalog.loadtest.yaml
export HUB_SECRET_KEY=zJcrbCiBSUaTSkQkK30tDMYDxeST8846fu9373dWE2I=
export HUB_ENCRYPTION_KEY=j7PECAceMkzWqN9vzEcgX4QruWG54odGOB0oeT0q3Ws=
export LOADTEST_UPSTREAM_URL=http://mock-upstream:8080/mcp
export LOADTEST_UPSTREAM_AUTHORIZE_URL=http://mock-upstream:8080/oauth/authorize
export LOADTEST_UPSTREAM_TOKEN_URL=http://mock-upstream:8080/oauth/token
export LOADTEST_UPSTREAM_REVOKE_URL=http://mock-upstream:8080/oauth/revoke
export LOADTEST_CLIENT_ID=loadtest-client LOADTEST_CLIENT_SECRET=loadtest-secret
python loadtest/tools/seed.py --users 60 --out loadtest/.seed/seed.json

# 4. Сценарии (SCALE=1 — полная нагрузка спеки, 0.1 — одна десятая)
k6 run -e SCALE=0.1 -e HUB_BASE=http://localhost:8000 loadtest/k6/wellknown.js

k6 run --summary-export=loadtest/.seed/summary-mcp.json \
  -e SCALE=0.1 -e HUB_BASE=http://localhost:8000 -e MOCK_URL=http://localhost:8080 \
  loadtest/k6/mcp.js
python loadtest/tools/overhead.py loadtest/.seed/summary-mcp.json

k6 run -e SCALE=0.1 -e HUB_BASE=http://localhost:8000 loadtest/k6/refresh_storm.js

# 5. Убрать контур вместе с данными
docker compose -f loadtest/docker-compose.loadtest.yml -p hubload down -v
```

`k6` ставится с хоста: `brew install k6`. Именно поэтому `HUB_PUBLIC_URL` контура —
`http://localhost:8000`: `aud` access-токенов Hub обязан совпадать с адресом, по
которому к нему обращается клиент.

## Режимы сценариев

| Переменная | Значение по умолчанию | Смысл |
|---|---|---|
| `SCALE` | `0.1` | множитель нагрузки: `1` — цифры спеки |
| `DURATION` | `60s` | длительность прогона |
| `SESSIONS` | `SCALE × 30000` | сколько виртуальных MCP-сессий открыть в `setup()` |
| `ACTIVE_RATIO` | `0.04` | доля активных сессий (спека: 3–5 %) |
| `RPS_PER_SESSION` | `1` | запросов в секунду на активную сессию |
| `MCP_RPS` | `SESSIONS × ACTIVE_RATIO × RPS_PER_SESSION` | целевой темп сценария (б) |
| `MODE` (`mcp.js`) | `steady` | `steady` — ожидаемый темп и пороги; `saturate` — открытый цикл, потолок реплики |
| `STORM_VUS` | `SCALE × 500` | одновременных цепочек refresh |
| `REFRESH_ROUNDS` | `5` | ротаций на цепочку в залповом режиме |
| `MODE` (`refresh_storm.js`) | `burst` | `burst` — залп из спеки; `sustained` — потолок по refresh/с |
| `MOCK_LATENCY_MS` | `0` | искусственная задержка мока |
| `MOCK_FAIL_RATE` | `0` | доля ответов 500 — для проверки circuit-breaker |

**Цепочки refresh одноразовые.** Каждый успешный `refresh_token` отзывает предыдущий
(R-O10), поэтому перед каждым прогоном сценария (в) данные надо пересевать
(`tools/seed.py`). Число VU не должно превышать «пользователей × alias'ов» из seed.

**Лимиты в контуре сняты** (`HUB_RATE_LIMIT_MCP`, `HUB_RATE_LIMIT_TOKEN`,
`HUB_RATE_LIMIT_REGISTER` = 1 000 000): измеряется пропускная способность, а не
работа rate-limiter'а. Прогон с боевыми лимитами — отдельный, значения
переопределяются в `docker-compose.loadtest.yml`.

## Защита от боевых адресов (D6-10)

1. **Каталог и конфигурация контура.** `catalog.loadtest.yaml` и
   `docker-compose.loadtest.yml` ссылаются только на `mock-upstream`; `HUB_LITELLM_BASE_URL`
   тоже указывает на мок, поэтому даже случайный вызов входа никуда не уйдёт.
2. **Проверка грепом.** `bash loadtest/tools/check_no_prod.sh` разбирает все файлы
   каталога `loadtest/`: каждый URL обязан вести на хост из allow-list, а
   корпоративные домены запрещены отдельным списком шаблонов. Ненулевой код
   возврата — прогон запрещён. Скрипт годится как шаг CI перед `k6 run`.
3. **Проверка во время запуска.** `assertLocal()` в `k6/lib/config.js` падает на этапе
   инициализации, если `HUB_BASE`/`MOCK_URL` указывают не на разрешённый хост;
   `_check_no_prod()` в `tools/seed.py` делает то же для `HUB_PUBLIC_URL` и
   `HUB_DATABASE_URL`.

Allow-list: `localhost`, `127.0.0.1`, `::1`, `0.0.0.0`, `hub`, `proxy`,
`mock-upstream`, `postgres`, `redis`, `example.invalid`.

## Результаты

Отчёт последнего прогона — `reports/loadtest-2026-08-20.md`.
