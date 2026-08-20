# Стенд Hub на Windows-ноутбуке (D6-01, D6-02, D6-04)

Пошаговая установка «сервера» для пилота: Docker Desktop с WSL2, корпоративный CA,
сертификат на DNS-имя ноутбука, запуск `docker compose` с Windows-override и
проверка скриптом `smoke.ps1`. Продуктивная выкладка — `deploy/helm` (см. `deploy/helm/README.md`),
типовые инциденты — `deploy/runbook.md`.

## 0. Что должно быть до начала

| | |
|---|---|
| ОС | Windows 10 21H2 / Windows 11, права локального администратора |
| Диск | ≥ 20 ГБ свободно (образы + том Postgres внутри WSL2) |
| ОЗУ | ≥ 8 ГБ, из них ≥ 4 ГБ можно отдать WSL2 |
| DNS-имя | A-запись на IP ноутбука, например `mcp-hub.corp.tander.ru` |
| Сертификат | серверный сертификат и ключ на это DNS-имя (D-1) |
| Корпоративный CA | цепочка `tander-ca-bundle.pem` |
| Сеть | доступ с ноутбука к LiteLLM, MCP AI Lab и целевым системам; доступ к ноутбуку по 443 (или 8443) |

## 1. Docker Desktop и WSL2

1. Включить компоненты Windows (PowerShell от администратора, затем перезагрузка):

   ```powershell
   dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
   dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
   wsl --set-default-version 2
   wsl --update
   ```

2. Установить Docker Desktop и в *Settings → General* включить
   **Use the WSL 2 based engine**, в *Settings → Resources → WSL Integration* —
   дистрибутив по умолчанию.
3. Ограничить аппетит WSL2, иначе он съедает всю память ноутбука. Файл
   `C:\Users\<логин>\.wslconfig`:

   ```ini
   [wsl2]
   memory=6GB
   processors=4
   swap=2GB
   ```

   Затем `wsl --shutdown` и запустить Docker Desktop заново.
4. В *Settings → General* включить **Start Docker Desktop when you log in** —
   вместе с `restart: unless-stopped` из Windows-override стенд поднимется сам
   после перезагрузки.

Rancher Desktop годится как замена: нужен движок `dockerd (moby)` и та же настройка WSL2.
Всё дальнейшее не меняется.

## 2. Код и каталоги

Распаковать (или клонировать) репозиторий **внутрь файловой системы Linux**, а не
на диск C:. Так bind-mount'ы не упираются в трансляцию прав NTFS, а Postgres
работает со своей скоростью:

```powershell
wsl
git clone <адрес репозитория> ~/opencode-mcp-hub
cd ~/opencode-mcp-hub/deploy
```

Если распаковывать всё-таки на `C:\`, обязательно проверить окончания строк
(см. «Типовые проблемы»).

## 3. Сертификаты и корпоративный CA

```bash
# внутри WSL, каталог deploy/
mkdir -p ca caddy/certs
cp /mnt/c/Users/<логин>/Downloads/tander-ca-bundle.pem ca/tander-ca-bundle.pem
cp /mnt/c/Users/<логин>/Downloads/mcp-hub.crt caddy/certs/server.crt
cp /mnt/c/Users/<логин>/Downloads/mcp-hub.key caddy/certs/server.key
chmod 600 caddy/certs/server.key
```

* `ca/tander-ca-bundle.pem` монтируется в контейнеры как `SSL_CERT_FILE` —
  без него Hub не достучится до LiteLLM и целевых систем по HTTPS.
* `caddy/certs/` — серверный сертификат **на то же имя**, что и `HUB_SITE_ADDRESS`
  (см. следующий шаг). Каталог в git не попадает (`.gitignore`).
* Если сертификат пришёл в PFX: `openssl pkcs12 -in mcp-hub.pfx -clcerts -nokeys -out server.crt`
  и `openssl pkcs12 -in mcp-hub.pfx -nocerts -nodes -out server.key`.
* В цепочку `server.crt` должны входить промежуточные сертификаты, иначе клиенты
  вне корпоративного домена получат ошибку доверия.

## 4. `.env`

```bash
cp .env.example .env
```

Заполнить в `.env` как минимум:

| Переменная | Значение на Windows-стенде |
|---|---|
| `HUB_PUBLIC_URL` | `https://mcp-hub.corp.tander.ru` — публичный адрес, он же `aud` токенов |
| `HUB_SITE_ADDRESS` | `mcp-hub.corp.tander.ru:8443` — имя сайта в Caddyfile, совпадает с сертификатом |
| `HUB_HTTPS_PORT` | `443`, либо другой порт, если 443 занят |
| `HUB_SECRET_KEY` | 32 случайных байта в base64 |
| `HUB_ENCRYPTION_KEY` | ключ Fernet, 44 символа urlsafe-base64 |
| `POSTGRES_PASSWORD` | пароль БД стенда |
| `HUB_LITELLM_BASE_URL` | адрес LiteLLM |
| `TAG_MCP_PUBLIC_URL` | `https://mcp-hub.corp.tander.ru/tag` — если поднимаете профиль `tag` |
| `*_OAUTH_CLIENT_ID` / `*_OAUTH_CLIENT_SECRET` | по мере выдачи OAuth-приложений |

Ключи генерируются так (в WSL):

```bash
python3 -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"   # HUB_SECRET_KEY
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" # HUB_ENCRYPTION_KEY
```

Пока для сервера не выдано OAuth-приложение (`*_OAUTH_CLIENT_ID` пуст), он считается
**ненастроенным**: не показывается в каталоге, не публикует метаданные и не появляется
в `/.well-known/opencode`. Это ожидаемое состояние, а не ошибка стенда.

## 5. Запуск

```bash
docker compose -f docker-compose.yml -f docker-compose.windows.yml up -d --build
# вместе с ТЭГ-MCP:
docker compose -f docker-compose.yml -f docker-compose.windows.yml --profile tag up -d --build
```

Что делает override `docker-compose.windows.yml` (D6-02):

* подставляет DNS-имя в `HUB_PUBLIC_URL`, `TAG_MCP_PUBLIC_URL` и `HUB_SITE_ADDRESS`;
* пробрасывает 443 дополнительно к 8443;
* включает `restart: unless-stopped` всем сервисам;
* держит `HUB_REPLICAS=1`: Docker Desktop без swarm игнорирует `deploy.replicas`,
  несколько реплик поднимаются через `docker compose ... up -d --scale hub=2`
  (общий Redis для этого уже есть);
* включает `HUB_TRUST_PROXY=true`: `X-Forwarded-For` проставляет caddy, других
  источников заголовка снаружи нет.

Проверка вручную:

```powershell
curl.exe -k https://localhost:8443/health
curl.exe -k https://localhost:8443/ready
curl.exe -k https://mcp-hub.corp.tander.ru/.well-known/opencode
```

## 6. Проверка стенда скриптом (D6-03)

```powershell
pwsh -File .\smoke.ps1 https://mcp-hub.corp.tander.ru
pwsh -File .\smoke.ps1 https://localhost:8443 -Insecure -TagBase https://localhost:8443/tag
pwsh -File .\smoke.ps1 https://mcp-hub.corp.tander.ru -ApiKey sk-... -External
```

Скрипт проверяет `/health`, `/ready`, `/.well-known/opencode` (включая ETag и 304),
метаданные OAuth и PRM по каждому facade-alias из каталога, закрытость `/api/catalog`
без ключа, редирект `/ui/*` на `/auth/login`, серии `hub_*` в `/metrics` и — при
`-TagBase` — ТЭГ-MCP. Итог печатается таблицей, при любом провале код возврата 1.
Без `-External` скрипт не обращается ни в одну внешнюю систему: `POST /cli/start`
и `GET /auth/login` ходят в настоящий LiteLLM и по умолчанию пропускаются.

Нужен PowerShell 7 (`winget install Microsoft.PowerShell`); в WSL тот же набор
проверок делает `bash ./smoke.sh https://mcp-hub.corp.tander.ru`.

## 7. Типовые проблемы

| Симптом | Причина | Что делать |
|---|---|---|
| `standard_init_linux.go: exec format error`, `$'\r': command not found` | скрипты выгружены с CRLF | `git config --global core.autocrlf input`, перевыгрузить; разово — `dos2unix smoke.sh` |
| Postgres не стартует, «could not fsync», всё тормозит | том БД оказался на bind-mount с NTFS | оставить named volume `pgdata` (так в базовом compose), репозиторий держать внутри WSL2 |
| `permission denied` на `/certs/server.key` | права на bind-mount c NTFS всегда 0777/root | держать `deploy/` в файловой системе WSL2; каталог монтируется `:ro`, менять права изнутри контейнера бесполезно |
| Контейнеры убиваются по OOM, WSL2 съел всю память | нет `.wslconfig` | задать `memory=6GB` (шаг 1.3), `wsl --shutdown` |
| `bind: address already in use` на 443 | 443 занят IIS, «Общий доступ к Интернету», Veeam или VPN-клиентом | `netstat -ano \| findstr :443`, освободить порт либо `HUB_HTTPS_PORT=8444` в `.env` |
| Клиенты не доходят до стенда | брандмауэр Windows | `New-NetFirewallRule -DisplayName "MCP Hub" -Direction Inbound -Protocol TCP -LocalPort 443,8443 -Action Allow` |
| Сборка образа падает на `pip install`, TLS-ошибки | антивирус/DPI подменяет сертификат | добавить корпоративный CA в образ (он уже монтируется в рантайме), внести Docker Desktop и WSL в исключения антивируса |
| `certificate is not valid for ...` у клиентов | `HUB_SITE_ADDRESS` не совпадает с CN/SAN | привести `.env` в соответствие сертификату и `docker compose ... up -d proxy` |
| `/auth/login` отдаёт 502 | нет сетевого доступа к LiteLLM | проверить `HUB_LITELLM_BASE_URL` и корпоративный CA; до починки smoke запускать без `-External` |
| В каталоге нет ни одного facade-сервера | не заданы `*_OAUTH_CLIENT_ID`/`SECRET` | ожидаемо до выдачи OAuth-приложений (см. шаг 4) |

Полезные команды:

```powershell
docker compose -f docker-compose.yml -f docker-compose.windows.yml ps
docker compose -f docker-compose.yml -f docker-compose.windows.yml logs -f hub
docker compose -f docker-compose.yml -f docker-compose.windows.yml restart hub proxy
wsl --shutdown          # перезапустить WSL2 целиком
```

## 8. Резервное копирование и восстановление (D6-04)

Критичны только данные Postgres: пользователи, ключи, подключения, зашифрованные
токены целевых систем, refresh-цепочки, аудит. **Redis не критичен** — там кэш,
MCP-сессии, окна rate-limit и состояние circuit-breaker; после потери Redis клиенты
переоткрывают сессии сами.

Вместе с дампом надо хранить `.env`: без `HUB_ENCRYPTION_KEY` токены систем в дампе
расшифровать невозможно, и после восстановления все подключения придётся авторизовать
заново. Дамп содержит персональные данные и зашифрованные токены — хранить рядом с
сертификатами, с тем же ограничением доступа.

Резервная копия:

```bash
# внутри WSL, каталог deploy/
mkdir -p backups
docker compose -f docker-compose.yml -f docker-compose.windows.yml exec -T postgres \
  pg_dump -U hub -d hub --format=custom > backups/hub-$(date +%Y%m%d-%H%M).dump
cp .env backups/env-$(date +%Y%m%d-%H%M).bak
```

Ежедневно по расписанию — через «Планировщик заданий» Windows, действие
`wsl -d <дистрибутив> -- bash -lc "cd ~/opencode-mcp-hub/deploy && ./backup.sh"`,
либо тем же однострочником. Хранить 7 последних копий.

Восстановление:

```bash
docker compose -f docker-compose.yml -f docker-compose.windows.yml stop hub
docker compose -f docker-compose.yml -f docker-compose.windows.yml exec -T postgres \
  dropdb -U hub --if-exists hub
docker compose -f docker-compose.yml -f docker-compose.windows.yml exec -T postgres \
  createdb -U hub hub
docker compose -f docker-compose.yml -f docker-compose.windows.yml exec -T postgres \
  pg_restore -U hub -d hub --no-owner < backups/hub-20260820-1200.dump
docker compose -f docker-compose.yml -f docker-compose.windows.yml start hub
./smoke.sh https://mcp-hub.corp.tander.ru
```

Схема БД приводится к `head` миграциями при старте Hub (`HUB_DB_AUTO_MIGRATE=true`),
поэтому дамп более старой версии восстанавливается штатно: сначала `pg_restore`,
затем запуск Hub. Проверять восстановление на копии стенда, а не на боевом.

Полная переустановка стенда с нуля (данные теряются):

```bash
docker compose -f docker-compose.yml -f docker-compose.windows.yml down -v
docker compose -f docker-compose.yml -f docker-compose.windows.yml up -d --build
```
