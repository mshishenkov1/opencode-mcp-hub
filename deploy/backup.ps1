<#
.SYNOPSIS
    Резервная копия стенда Hub (D6-04): дамп Postgres + копия .env, с ротацией.

.DESCRIPTION
    Критичны только данные Postgres (пользователи, ключи, подключения, зашифрованные
    токены целевых систем, refresh-цепочки, аудит) и .env: без HUB_ENCRYPTION_KEY
    токены в дампе расшифровать невозможно. Redis не критичен — там кэш и сессии.

    Дамп снимается внутри контейнера и забирается docker compose cp: так на Windows
    не участвует перенаправление вывода PowerShell, которое портит двоичный файл.

    Код возврата: 0 — копия снята и проверена, 1 — любая ошибка. Годится для
    «Планировщика заданий»: молчаливых провалов нет.

.EXAMPLE
    pwsh -File .\backup.ps1
    pwsh -File .\backup.ps1 -Keep 14 -BackupDir D:\backup\hub
    pwsh -File .\backup.ps1 -Project hubi3 -ComposeFiles docker-compose.yml

.NOTES
    ВОССТАНОВЛЕНИЕ (той же командой compose, что и запуск стенда):
      docker compose -f docker-compose.yml -f docker-compose.windows.yml stop hub
      docker compose ... exec -T postgres dropdb   -U hub --if-exists hub
      docker compose ... exec -T postgres createdb -U hub hub
      cmd /c "docker compose ... exec -T postgres pg_restore -U hub -d hub --no-owner < backups\hub-20260820-1200.dump"
      docker compose ... start hub
      pwsh -File .\smoke.ps1 https://mcp-hub.corp.tander.ru
    Схема приводится к head миграциями при старте Hub (HUB_DB_AUTO_MIGRATE=true),
    поэтому дамп более старой версии восстанавливается штатно. Проверять
    восстановление на копии стенда, а не на боевом.
#>
[CmdletBinding()]
param(
    [string]   $BackupDir    = "",
    [int]      $Keep         = 7,
    [string[]] $ComposeFiles = @("docker-compose.yml", "docker-compose.windows.yml"),
    [string]   $Project      = "",
    [string]   $Database     = "hub",
    [string]   $DbUser       = "hub"
)

$ErrorActionPreference = "Stop"
# PowerShell 7.4 по умолчанию превращает ненулевой код внешней команды в
# терминирующую ошибку; здесь коды разбираются вручную.
$PSNativeCommandUseErrorActionPreference = $false

$deployDir = $PSScriptRoot
Set-Location $deployDir

function Die {
    param([string]$Message)
    [Console]::Error.WriteLine($Message)
    exit 1
}

if (-not $BackupDir) { $BackupDir = Join-Path $deployDir "backups" }
if ($Keep -lt 1) { Die "Keep должен быть >= 1, получено: $Keep" }

$compose = @("compose")
foreach ($f in $ComposeFiles) {
    if (-not (Test-Path (Join-Path $deployDir $f))) {
        Die "Не найден файл compose: $(Join-Path $deployDir $f) (см. параметр -ComposeFiles)"
    }
    $compose += @("-f", $f)
}
if ($Project) { $compose += @("-p", $Project) }

function Invoke-Compose {
    param([string[]] $ComposeArgs)
    $out = & docker @($compose + $ComposeArgs) 2>&1
    return @{ Code = $LASTEXITCODE; Output = $out }
}

$ps = Invoke-Compose @("ps", "-q", "postgres")
if ($ps.Code -ne 0 -or -not ($ps.Output -join "").Trim()) {
    Die "Контейнер postgres не запущен: снять дамп нечем. Проверьте: docker $($compose -join ' ') ps"
}

New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$dump = Join-Path $BackupDir "hub-$stamp.dump"
$inContainer = "/tmp/hub-$stamp.dump"

Write-Host "Дамп базы $Database → $dump"
$r = Invoke-Compose @("exec", "-T", "postgres", "pg_dump", "-U", $DbUser, "-d", $Database, "--format=custom", "-f", $inContainer)
if ($r.Code -ne 0) {
    Die "pg_dump завершился с ошибкой — копия не создана.`n$($r.Output -join "`n")"
}

$r = Invoke-Compose @("cp", "postgres:$inContainer", $dump)
Invoke-Compose @("exec", "-T", "postgres", "rm", "-f", $inContainer) | Out-Null
if ($r.Code -ne 0) {
    Die "Не удалось забрать дамп из контейнера.`n$($r.Output -join "`n")"
}

# Проверка: непустой файл в формате custom (сигнатура PGDMP).
$magic = ""
if (Test-Path $dump) {
    $bytes = Get-Content -Path $dump -AsByteStream -TotalCount 5 -ErrorAction SilentlyContinue
    if ($bytes) { $magic = -join ($bytes | ForEach-Object { [char]$_ }) }
}
if ($magic -ne "PGDMP") {
    Remove-Item -Force -ErrorAction SilentlyContinue $dump
    Die "Дамп пуст или не в формате custom — копия не создана."
}

# .env хранится рядом: без HUB_ENCRYPTION_KEY дамп бесполезен.
$envFile = Join-Path $deployDir ".env"
if (Test-Path $envFile) {
    Copy-Item $envFile (Join-Path $BackupDir "env-$stamp.bak") -Force
} else {
    Write-Warning "$envFile не найден, копия ключей не сделана."
}

# Ротация: оставляем $Keep самых свежих копий каждого вида.
foreach ($pattern in @("hub-*.dump", "env-*.bak")) {
    $files = Get-ChildItem -Path $BackupDir -Filter $pattern -File | Sort-Object Name -Descending
    if ($files.Count -gt $Keep) {
        foreach ($old in $files[$Keep..($files.Count - 1)]) {
            Remove-Item -Force $old.FullName
            Write-Host "Удалена старая копия: $($old.Name)"
        }
    }
}

$size = (Get-Item $dump).Length
$kept = (Get-ChildItem -Path $BackupDir -Filter "hub-*.dump" -File).Count
Write-Host "РЕЗУЛЬТАТ: OK — $dump ($size байт), копий в ${BackupDir}: $kept (храним $Keep)"
exit 0
