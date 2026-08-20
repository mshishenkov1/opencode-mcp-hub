<#
.SYNOPSIS
    Проверка развёрнутого стенда Hub на Windows (D6-03).

.DESCRIPTION
    То же, что deploy/smoke.sh: по одному запросу на проверку, итог таблицей,
    код возврата 1 при любом провале. Требуется PowerShell 7+ (pwsh).

    Без -External скрипт не обращается ни к одной внешней системе: проверяются
    только Hub и ТЭГ-MCP на самом стенде.

.PARAMETER HubBase
    Базовый адрес Hub (позиционный параметр). По умолчанию — $env:HUB_BASE или
    https://localhost:8443.

.EXAMPLE
    ./smoke.ps1
    ./smoke.ps1 https://mcp-hub.corp.tander.ru
    ./smoke.ps1 -Insecure -TagBase https://localhost:8443/tag
    ./smoke.ps1 -ApiKey sk-... -External
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$HubBase = $(if ($env:HUB_BASE) { $env:HUB_BASE } else { 'https://localhost:8443' }),
    [string]$TagBase = $env:TAG_BASE,
    [string]$ApiKey = $env:HUB_API_KEY,
    [string[]]$Aliases = @(),
    [int]$TimeoutSec = 15,
    # Самоподписанный сертификат стенда
    [switch]$Insecure,
    # Выполнять проверки, обращающиеся в LiteLLM (POST /cli/start, GET /auth/login)
    [switch]$External
)

$ErrorActionPreference = 'Stop'
$HubBase = $HubBase.TrimEnd('/')
if ($TagBase) { $TagBase = $TagBase.TrimEnd('/') }
if (-not $HubBase) { Write-Error 'Не задан базовый URL Hub'; exit 2 }

$script:Rows = [System.Collections.Generic.List[object]]::new()

function Add-Row {
    param([string]$Status, [string]$Name, [string]$Want, [string]$Got)
    $script:Rows.Add([pscustomobject]@{ ИТОГ = $Status; ПРОВЕРКА = $Name; ОЖИДАНИЕ = $Want; ФАКТ = $Got })
}

function Invoke-Probe {
    <# Один запрос. Возвращает @{Code=<int>; Body=<string>; Headers=<hashtable>} #>
    param(
        [string]$Method = 'GET',
        [Parameter(Mandatory)][string]$Url,
        [hashtable]$Headers = @{},
        [string]$Body
    )
    $params = @{
        Method             = $Method
        Uri                = $Url
        Headers            = $Headers
        TimeoutSec         = $TimeoutSec
        SkipHttpErrorCheck = $true
        MaximumRedirection = 0
        ErrorAction        = 'Stop'
    }
    if ($Insecure) { $params.SkipCertificateCheck = $true }
    if ($PSBoundParameters.ContainsKey('Body') -and $Body) {
        $params.Body = $Body
        $params.ContentType = 'application/json'
    }
    try {
        $r = Invoke-WebRequest @params
        return @{ Code = [int]$r.StatusCode; Body = [string]$r.Content; Headers = $r.Headers }
    }
    catch {
        # MaximumRedirection=0 в PowerShell 7 отдаёт 3xx обычным ответом, но сетевые
        # ошибки приходят исключением. Код 0 означает «не достучались».
        $resp = $_.Exception.Response
        if ($resp) { return @{ Code = [int]$resp.StatusCode; Body = ''; Headers = @{} } }
        return @{ Code = 0; Body = $_.Exception.Message; Headers = @{} }
    }
}

function Get-Header {
    param([hashtable]$Headers, [string]$Name)
    if (-not $Headers) { return '' }
    foreach ($k in $Headers.Keys) {
        if ($k -ieq $Name) { return [string](@($Headers[$k])[0]) }
    }
    return ''
}

function Test-Code {
    param([string]$Name, [int[]]$Want, [string]$Method = 'GET', [string]$Url,
          [hashtable]$Headers = @{}, [string]$Body)
    $probeArgs = @{ Method = $Method; Url = $Url; Headers = $Headers }
    if ($Body) { $probeArgs.Body = $Body }
    $res = Invoke-Probe @probeArgs
    $wantText = 'HTTP ' + ($Want -join ' или ')
    if ($Want -contains $res.Code) {
        Add-Row 'OK' $Name $wantText "HTTP $($res.Code)"
    }
    else {
        Add-Row 'FAIL' $Name $wantText "HTTP $($res.Code)"
    }
    return $res
}

Write-Host "Стенд:   $HubBase"
if ($TagBase) { Write-Host "ТЭГ-MCP: $TagBase" }
Write-Host ''

# --- 1. Живость и готовность ------------------------------------------------
$health = Test-Code -Name 'GET /health' -Want 200 -Url "$HubBase/health"
if ($health.Body -match '"status"\s*:\s*"ok"') {
    Add-Row 'OK' 'GET /health: status=ok' 'status=ok' 'ok'
}
else {
    Add-Row 'FAIL' 'GET /health: status=ok' 'status=ok' 'нет status=ok'
}
Test-Code -Name 'GET /ready' -Want 200 -Url "$HubBase/ready" | Out-Null

# --- 2. Конфигурация клиента ------------------------------------------------
$wk = Invoke-Probe -Url "$HubBase/.well-known/opencode"
if ($wk.Code -eq 200 -and $wk.Body -match '"remote_config"') {
    Add-Row 'OK' 'GET /.well-known/opencode' 'JSON с remote_config' 'получен'
}
else {
    Add-Row 'FAIL' 'GET /.well-known/opencode' 'JSON с remote_config' "HTTP $($wk.Code)"
}

$etag = Get-Header -Headers $wk.Headers -Name 'ETag'
if ($etag) {
    $notmod = Invoke-Probe -Url "$HubBase/.well-known/opencode" -Headers @{ 'If-None-Match' = $etag }
    if ($notmod.Code -eq 304) {
        Add-Row 'OK' 'ETag /.well-known/opencode' 'HTTP 304' 'HTTP 304'
    }
    else {
        Add-Row 'FAIL' 'ETag /.well-known/opencode' 'HTTP 304' "HTTP $($notmod.Code)"
    }
}
else {
    Add-Row 'FAIL' 'ETag /.well-known/opencode' 'заголовок ETag' 'нет заголовка'
}

# --- 3. Метаданные OAuth ----------------------------------------------------
$asMeta = Test-Code -Name 'GET /.well-known/oauth-authorization-server' -Want 200 `
    -Url "$HubBase/.well-known/oauth-authorization-server"

if (-not $Aliases -or $Aliases.Count -eq 0) {
    # Источник истины по facade-серверам стенда — scopes_supported AS-метаданных.
    $Aliases = @([regex]::Matches($asMeta.Body, '"(?<a>[A-Za-z0-9_-]+):readonly"') |
        ForEach-Object { $_.Groups['a'].Value } | Sort-Object -Unique)
}
if (-not $Aliases -or $Aliases.Count -eq 0) {
    # Запасной источник: адреса вида <публичный-адрес>/mcp/<alias> в /.well-known/opencode.
    $Aliases = @([regex]::Matches($wk.Body, '"url":"[^"]*/mcp/(?<a>[A-Za-z0-9_-]+)"') |
        ForEach-Object { $_.Groups['a'].Value } | Sort-Object -Unique)
}

if (-not $Aliases -or $Aliases.Count -eq 0) {
    # На стенде без выданных OAuth-приложений facade-серверы unconfigured и в метаданных
    # не публикуются — ожидаемое состояние, а не отказ (см. README-windows.md).
    Add-Row 'SKIP' 'Метаданные facade-серверов' 'хотя бы один alias' `
        'нет настроенных facade-серверов (не заданы *_OAUTH_CLIENT_ID/SECRET)'
}
else {
    foreach ($a in $Aliases) {
        Test-Code -Name "AS-метаданные /mcp/$a" -Want 200 `
            -Url "$HubBase/.well-known/oauth-authorization-server/mcp/$a" | Out-Null
        Test-Code -Name "PRM /mcp/$a" -Want 200 `
            -Url "$HubBase/.well-known/oauth-protected-resource/mcp/$a" | Out-Null
    }
    Test-Code -Name 'PRM /mcp/__нет-такого__' -Want 404 `
        -Url "$HubBase/.well-known/oauth-protected-resource/mcp/__нет-такого__" | Out-Null
}

# --- 4. Каталог и права -----------------------------------------------------
if ($ApiKey) {
    $auth = @{ Authorization = "Bearer $ApiKey" }
    Test-Code -Name 'GET /api/catalog (с ключом)' -Want 200 -Url "$HubBase/api/catalog" -Headers $auth | Out-Null
    Test-Code -Name 'GET /api/me (с ключом)' -Want 200 -Url "$HubBase/api/me" -Headers $auth | Out-Null
    Test-Code -Name 'GET /remote-config (с ключом)' -Want 200 -Url "$HubBase/remote-config" -Headers $auth | Out-Null
}
else {
    Test-Code -Name 'GET /api/catalog (без ключа)' -Want 401 -Url "$HubBase/api/catalog" | Out-Null
    Add-Row 'SKIP' 'GET /api/catalog (с ключом)' 'HTTP 200' 'ключ не задан (-ApiKey)'
}

# --- 5. Веб-интерфейс -------------------------------------------------------
# Страницы /ui/* без сессии отдают 302 на /auth/login?next=… (hub/web.py: login_redirect).
foreach ($uiPath in @('/ui/connections', '/ui/servers/gitlab')) {
    $ui = Invoke-Probe -Url "$HubBase$uiPath"
    $loc = Get-Header -Headers $ui.Headers -Name 'Location'
    if ($ui.Code -eq 302 -and $loc.StartsWith('/auth/login')) {
        Add-Row 'OK' "GET $uiPath без сессии" '302 на /auth/login' "HTTP 302 -> $loc"
    }
    else {
        $shown = if ($loc) { $loc } else { '—' }
        Add-Row 'FAIL' "GET $uiPath без сессии" '302 на /auth/login' "HTTP $($ui.Code) -> $shown"
    }
}

# --- 6. Метрики -------------------------------------------------------------
$metrics = Invoke-Probe -Url "$HubBase/metrics"
$series = ([regex]::Matches($metrics.Body, '(?m)^hub_')).Count
if ($metrics.Code -eq 200 -and $series -gt 0) {
    Add-Row 'OK' 'GET /metrics' 'серии hub_*' "$series строк"
}
else {
    Add-Row 'FAIL' 'GET /metrics' 'серии hub_*' "HTTP $($metrics.Code), серий $series"
}

# --- 7. ТЭГ-MCP (профиль tag) -----------------------------------------------
if ($TagBase) {
    Test-Code -Name 'tag-mcp GET /health' -Want 200 -Url "$TagBase/health" | Out-Null
    Test-Code -Name 'tag-mcp PRM' -Want 200 -Url "$TagBase/.well-known/oauth-protected-resource" | Out-Null
}
else {
    Add-Row 'SKIP' 'tag-mcp /health' 'HTTP 200' 'адрес не задан (-TagBase)'
    Add-Row 'SKIP' 'tag-mcp PRM' 'HTTP 200' 'адрес не задан (-TagBase)'
}

# --- 8. Проверки, обращающиеся в LiteLLM (только с -External) ---------------
if ($External) {
    Test-Code -Name 'POST /cli/start' -Want 200 -Method 'POST' -Url "$HubBase/cli/start" `
        -Body '{"client":"smoke"}' | Out-Null
    Test-Code -Name 'GET /auth/login' -Want @(200, 302) -Url "$HubBase/auth/login" | Out-Null
}
else {
    Add-Row 'SKIP' 'POST /cli/start' 'HTTP 200' 'нужен -External (ходит в LiteLLM)'
    Add-Row 'SKIP' 'GET /auth/login' 'HTTP 200 или 302' 'нужен -External (ходит в LiteLLM)'
}

# --- Итог -------------------------------------------------------------------
$script:Rows | Format-Table -AutoSize | Out-String -Width 200 | Write-Host

$pass = @($script:Rows | Where-Object ИТОГ -EQ 'OK').Count
$fail = @($script:Rows | Where-Object ИТОГ -EQ 'FAIL').Count
$skip = @($script:Rows | Where-Object ИТОГ -EQ 'SKIP').Count
Write-Host "Успешно: $pass, провалено: $fail, пропущено: $skip"

if ($fail -gt 0) {
    Write-Error 'РЕЗУЛЬТАТ: ПРОВАЛ' -ErrorAction Continue
    exit 1
}
Write-Host 'РЕЗУЛЬТАТ: OK'
exit 0
