#Requires -Version 5.1
<#
.SYNOPSIS
    Установщик OpenCode Magnit для Windows (installers/docs/spec.md, N5-I1…N5-I12, N5-R*, N5-C*).

.DESCRIPTION
    Ставит корпоративный CA, CLI-бинарник и (если есть в пакете) Desktop. Ничего не спрашивает,
    ключей не просит, пользовательский конфиг не меняет, в сеть не ходит (N5-P6).
    Файл рассчитан на загрузку через dot-source без выполнения действий (N5-T2):
    исполняемый вход внизу срабатывает только при обычном запуске.
    Совместимость: PowerShell 5.1 — без тернарного оператора, без операторов объединения
    с null, без параллельного перебора, без командлета склейки строк, без переменной
    стилей вывода (N5-T5).
    Кодировка файла: UTF-8 с BOM (N5-T6).
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Check,
    [switch]$Uninstall,
    [switch]$Purge,
    [string]$Prefix,
    [switch]$NoDesktop,
    [switch]$NoLaunch,
    [switch]$Quiet,
    [switch]$Help,
    [switch]$ShowVersion,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

# =====================================================================================
# Константы: коды выхода (N5-I1)
# =====================================================================================
$script:ExitOk = 0
$script:ExitFail = 1
$script:ExitArgs = 2
$script:ExitEnv = 3
$script:ExitHash = 4
$script:ExitPerm = 5
$script:ExitBusy = 6
$script:ExitDiff = 7

# =====================================================================================
# Константы: имена и маркеры
# =====================================================================================
$script:OptQuiet = $false
$script:ManifestPath = ''
# Переопределение корня пакета — только для тестов, подменяющих раскладку (N5-T2).
$script:PackageRootOverride = ''
$script:Product = 'opencode-magnit'
$script:SchemaVersion = 1
$script:EnvName = 'NODE_EXTRA_CA_CERTS'

# =====================================================================================
# Константы: строки вывода — единый источник для тестов (N5-C2, N5-I12).
# Формулировки — часть контракта: правятся здесь, в тестах и в CI одновременно.
# =====================================================================================
$script:MsgDone = 'Готово: OpenCode {0} установлен.'
$script:MsgSumBin = '  Бинарник: {0}'
$script:MsgSumCa = '  CA: {0}'
$script:MsgSumDesktop = '  Desktop: {0}'
$script:MsgSumConfig = '  Конфиг: {0} (не изменялся)'
$script:MsgSumHub = '  Hub: {0}'
$script:MsgNext = 'Дальше:'
$script:MsgNextTerminal = '  1. Откройте новое окно терминала'
$script:MsgNextRun = '  2. Запустите: opencode'
$script:MsgNextSso = '  Вход по корпоративному SSO предложат при первом запуске.'

$script:MsgCaSame = 'CA: уже установлен'
$script:MsgCaInstalled = 'CA: установлен ({0})'
$script:MsgCaReplaced = 'CA: обновлён ({0})'
$script:MsgBinSame = 'Бинарник: уже установлен ({0})'
$script:MsgBinOther = 'Бинарник: другая версия (установлена {0}, в пакете {1})'
$script:MsgBinInstalled = 'Бинарник: установлен ({0})'
$script:MsgBinBackup = 'Бинарник: предыдущая версия сохранена ({0})'
$script:MsgEnvSame = 'NODE_EXTRA_CA_CERTS: уже задан'
$script:MsgEnvSet = 'NODE_EXTRA_CA_CERTS: задан ({0})'
$script:MsgPathAdded = 'PATH: добавлен {0}'
$script:MsgPathSame = 'PATH: уже содержит {0}'
$script:MsgPathWarn = 'Предупреждение: не удалось разослать WM_SETTINGCHANGE — новые процессы увидят PATH после перезахода'
$script:MsgConfigKept = 'Конфиг: {0} (не изменялся)'
$script:MsgConfigBackup = 'Конфиг: создана резервная копия ({0})'
$script:MsgConfigBackupKept = 'Конфиг: резервная копия уже существует ({0})'

$script:MsgDesktopNone = 'Desktop: не входит в пакет'
$script:MsgDesktopSkipped = 'Desktop: пропущен (--no-desktop)'
$script:MsgDesktopInstalled = 'Desktop: установлен ({0})'
$script:MsgDesktopManualAt = 'Desktop: запустите установщик вручную: {0}'
$script:MsgDesktopError = 'Desktop: ошибка (код {0})'
$script:MsgDesktopManualRemove = 'Desktop: удалите вручную через «Приложения и возможности»'
$script:MsgDesktopParseFail = 'Desktop: строка деинсталляции разобрана неоднозначно ({0}), запуск отменён'

$script:MsgCheckPkg = 'Пакет: opencode-magnit {0} ({1}-{2})'
$script:MsgCheckManifest = 'Манифест: корректен'
$script:MsgCheckBinOk = 'Бинарник: установлен {0} ({1})'
$script:MsgCheckBinOther = 'Бинарник: другая версия (установлена {0}, в пакете {1})'
$script:MsgCheckBinNoVer = 'Бинарник: установлен, версия не определена ({0})'
$script:MsgCheckBinNone = 'Бинарник: не установлен'
$script:MsgCheckPathYes = 'PATH: содержит {0}'
$script:MsgCheckPathNo = 'PATH: не содержит {0}'
$script:MsgCheckCaOk = 'CA: установлен ({0})'
$script:MsgCheckCaDiff = 'CA: отличается от пакета ({0})'
$script:MsgCheckCaNone = 'CA: не установлен'
$script:MsgCheckEnvOk = 'NODE_EXTRA_CA_CERTS: задан ({0})'
$script:MsgCheckEnvOther = 'NODE_EXTRA_CA_CERTS: задан другой путь ({0})'
$script:MsgCheckEnvNone = 'NODE_EXTRA_CA_CERTS: не задан'
$script:MsgCheckDesktopOk = 'Desktop: установлен ({0})'
$script:MsgCheckDesktopNone = 'Desktop: не установлен'
$script:MsgCheckConfig = 'Конфиг: {0} (не изменяется установщиком)'
$script:MsgCheckHub = 'Hub: {0}'
$script:MsgCheckSumOk = 'Итог: всё установлено'
$script:MsgCheckSumNeed = 'Итог: требуется установка'

$script:MsgPlanHead = 'План установки OpenCode {0} ({1}-{2}) — изменения не вносятся:'
$script:MsgPlanUninstallHead = 'План удаления OpenCode {0} — изменения не вносятся:'
$script:MsgPlanStep = '  {0}. {1}'
$script:MsgPlanMkdir = 'Создать каталог: {0}'
$script:MsgPlanCa = 'Скопировать CA: {0} -> {1}'
$script:MsgPlanBin = 'Скопировать бинарник: {0} -> {1}'
$script:MsgPlanBinBackup = 'Сохранить прежний бинарник: {0} -> {1}'
$script:MsgPlanEnv = 'Задать переменную пользователя {0}={1}'
$script:MsgPlanPath = 'Добавить в PATH пользователя: {0}'
$script:MsgPlanDesktopRun = 'Запустить установщик Desktop: {0} {1}'
$script:MsgPlanConfig = 'Конфиг: {0} не изменяется; резервная копия {1}'
$script:MsgPlanCorpStatus = 'Показать состояние: {0} corp status'
$script:MsgPlanRemove = 'Удалить: {0}'
$script:MsgPlanEnvUnset = 'Снять переменную {0} (значение наше)'
$script:MsgPlanPathRemove = 'Убрать из PATH пользователя: {0}'

$script:MsgUninstallHead = 'Удаление OpenCode Magnit {0}:'
$script:MsgUninstallRemoved = '{0}: удалён ({1})'
$script:MsgUninstallAbsent = '{0}: не найден'
$script:MsgUninstallForeignEnv = 'NODE_EXTRA_CA_CERTS: значение чужое, переменная не изменена'
$script:MsgUninstallDone = 'Готово: OpenCode удалён.'
$script:MsgPurgeHead = 'Удаляются пользовательские данные:'
$script:MsgPurgeItem = '  {0}'
$script:MsgPurgeRemoved = 'Данные: удалено ({0})'
$script:MsgPurgeAbsent = 'Данные: не найдено ({0})'
$script:ObjBin = 'Бинарник'
$script:ObjBinBak = 'Резервная копия бинарника'
$script:ObjCa = 'CA'
$script:ObjDesktop = 'Desktop'
$script:ObjPath = 'Каталог в PATH'

$script:MsgErrUnknownArg = 'Неизвестный аргумент: {0}'
$script:MsgErrExclusive = 'Взаимоисключающие режимы: {0}'
$script:MsgErrPurgeAlone = '--purge применяется только вместе с --uninstall'
$script:MsgErrManifestMissing = 'Манифест не найден: {0}'
$script:MsgErrManifestParse = 'Манифест не разбирается как JSON: {0}'
$script:MsgErrManifestField = 'Манифест {0}: поле {1} — {2}'
$script:MsgErrFieldRequired = 'обязательное поле отсутствует или пусто'
$script:MsgErrFieldSchema = 'ожидается значение 1'
$script:MsgErrFieldProduct = 'ожидается значение opencode-magnit'
$script:MsgErrFieldEnum = 'недопустимое значение: {0}'
$script:MsgErrFieldSha = 'требуется 64 символа [0-9a-f]'
$script:MsgErrFieldPath = 'недопустимый путь: {0}'
$script:MsgErrFieldNoFile = 'файл отсутствует в пакете: {0}'
$script:MsgErrFieldCli = 'требуется ровно один артефакт kind="cli", найдено {0}'
$script:MsgErrFieldDesktop = 'допускается не более одного артефакта kind="desktop", найдено {0}'
$script:MsgErrHashMismatch = 'Несовпадение sha256: {0} (ожидался {1}, фактический {2})'
$script:MsgErrOsMismatch = 'Пакет собран для {0}-{1}, система — {2}-{3}'
$script:MsgErrOsUnsupported = 'Установщик рассчитан на Windows x64; текущая система — {0}-{1}'
$script:MsgErrPsVersion = 'Требуется PowerShell 5.1 или новее, найдена версия {0}'
$script:MsgErrBusy = 'файл занят: закройте OpenCode и повторите запуск'
$script:MsgErrNoWrite = 'Нет прав на запись в каталог: {0}'
$script:MsgErrPurgeUnsafe = 'Небезопасный путь в purge_paths: {0} (допустимы только пути внутри профиля пользователя)'
$script:MsgErrPurgeRejected = 'Список purge_paths отвергнут целиком, ничего не удалено'
$script:MsgVersionLine = 'opencode-magnit {0} ({1}-{2})'

$script:HelpText = @'
Установщик OpenCode Magnit (Windows).

Использование: install.bat [флаги]   или   powershell -File install.ps1 [флаги]

Флаги:
  -DryRun            напечатать план шагов, ничего не менять
  -Check             проверить состояние среды, ничего не менять
  -Uninstall         удалить установленное
  -Purge             вместе с -Uninstall — удалить и пользовательские данные
  -Prefix <каталог>  корень установки вместо %LOCALAPPDATA%\Programs\opencode
  -NoDesktop         пропустить шаг Desktop
  -NoLaunch          не выполнять финальный opencode corp status
  -Quiet             печатать только итог и ошибки
  -Help              показать эту справку
  -ShowVersion       напечатать версию пакета из манифеста

Коды выхода: 0 успех, 1 ошибка выполнения, 2 аргументы или манифест, 3 несовместимая среда,
4 несовпадение sha256, 5 нет прав на запись, 6 файл занят, 7 -Check нашёл расхождения.

Установщик работает только с файлами пакета: ничего не загружает и ключей не запрашивает.
'@

# =====================================================================================
# Вывод и ошибки
# =====================================================================================
# Вывод идёт прямо в поток консоли, а не в конвейер: иначе строки отчёта смешались бы
# с возвращаемыми значениями функций (код выхода). В тестах подменяется моком (N5-T2).
function Write-Line {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)
    [Console]::Out.WriteLine($Text)
}

function Write-Say {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)
    if (-not $script:OptQuiet) {
        [Console]::Out.WriteLine($Text)
    }
}

function Write-ErrorLine {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)
    [Console]::Error.WriteLine($Text)
}

# Прерывает выполнение с заданным кодом. Пустое сообщение — штатный выход (например --check = 7).
function Invoke-Failure {
    param(
        [Parameter(Mandatory = $true)][int]$Code,
        [string]$Message = ''
    )
    $ex = New-Object System.Exception $Message
    $ex.Data['Code'] = $Code
    throw $ex
}

# =====================================================================================
# Работа с JSON-манифестом (N5-P3, N5-P4)
# =====================================================================================
function Get-Prop {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    $prop = $Object.PSObject.Properties[$Name]
    if ($null -eq $prop) { return $null }
    return $prop.Value
}

function Test-Sha256Value {
    param([string]$Value)
    if ([string]::IsNullOrEmpty($Value)) { return $false }
    return ($Value -match '^[0-9a-fA-F]{64}$')
}

# Путь внутри пакета: относительный, без "..", без буквы диска, без обратного слэша (N5-P3).
# Отвергаются также ".", "./x", "x/." и завершающий "/" — формы, при которых "<каталог>/<значение>"
# схлопывается в сам каталог (паритет с is_safe_pkg_path в common/install-posix.sh).
# Ревизия 1.5 (N5-P4, N5-I13): набор символов задан БЕЛЫМ списком — A-Za-z0-9, точка,
# подчёркивание, дефис и разделитель "/". Тот же набор применяет POSIX-ветка, поэтому пакет,
# принятый на macOS/Linux, принимается и на Windows. Проверка "\z", а не "$": в .NET "$"
# совпадает и перед завершающим переводом строки, то есть "a.pem`n" прошло бы whitelist.
# Сегменты "." и ".." проверяются ПОСЕГМЕНТНО, а не поиском подстроки: "opencode..pem" — законное
# имя файла, и раньше оно принималось POSIX-веткой и отвергалось здесь (расхождение паритета).
function Test-PackagePath {
    param([string]$Value)
    if ([string]::IsNullOrEmpty($Value)) { return $false }
    if ($Value -notmatch '\A[A-Za-z0-9._/-]+\z') { return $false }
    if ($Value.StartsWith('/')) { return $false }
    if ($Value.EndsWith('/')) { return $false }
    foreach ($segment in $Value.Split('/')) {
        if ($segment -eq '.' -or $segment -eq '..') { return $false }
    }
    return $true
}

# Имя приложения (artifacts[].app_name): одно имя без разделителей пути и самоссылок и без
# подстановочных символов — значение участвует в сопоставлении записей реестра (-like) и в путях,
# поэтому проверяется строже пути внутри пакета (N5-P4, N5-R1). Паритет с is_safe_app_name.
# Отличие набора символов от Test-PackagePath ровно одно: допускается пробел U+0020, но не
# первым и не последним символом («OpenCode Magnit.app»); разделитель "/" не допускается.
function Test-AppName {
    param([string]$Value)
    if ([string]::IsNullOrEmpty($Value)) { return $false }
    if ([string]::IsNullOrWhiteSpace($Value)) { return $false }
    if ($Value -notmatch '\A[A-Za-z0-9._ -]+\z') { return $false }
    if ($Value.StartsWith(' ') -or $Value.EndsWith(' ')) { return $false }
    if ($Value -eq '.' -or $Value -eq '..') { return $false }
    return $true
}

function Invoke-ManifestFieldFailure {
    param([string]$Path, [string]$Field, [string]$Reason)
    Invoke-Failure -Code $script:ExitArgs -Message ($script:MsgErrManifestField -f $Path, $Field, $Reason)
}

function Read-Manifest {
    param([Parameter(Mandatory = $true)][string]$PackageRoot)

    $manifestPath = Join-Path (Join-Path $PackageRoot 'common') 'manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        Invoke-Failure -Code $script:ExitArgs -Message ($script:MsgErrManifestMissing -f $manifestPath)
    }
    $raw = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8
    $manifest = $null
    try {
        $manifest = $raw | ConvertFrom-Json
    } catch {
        Invoke-Failure -Code $script:ExitArgs -Message ($script:MsgErrManifestParse -f $manifestPath)
    }
    if ($null -eq $manifest -or $manifest -isnot [System.Management.Automation.PSCustomObject]) {
        Invoke-Failure -Code $script:ExitArgs -Message ($script:MsgErrManifestParse -f $manifestPath)
    }

    $schema = Get-Prop $manifest 'schema'
    if ($null -eq $schema -or [int]$schema -ne $script:SchemaVersion) {
        Invoke-ManifestFieldFailure -Path $manifestPath -Field 'schema' -Reason $script:MsgErrFieldSchema
    }
    if ((Get-Prop $manifest 'product') -ne $script:Product) {
        Invoke-ManifestFieldFailure -Path $manifestPath -Field 'product' -Reason $script:MsgErrFieldProduct
    }
    foreach ($field in @('version', 'os', 'arch', 'hub_url', 'built_at', 'source_release')) {
        $value = Get-Prop $manifest $field
        if ([string]::IsNullOrEmpty($value)) {
            Invoke-ManifestFieldFailure -Path $manifestPath -Field $field -Reason $script:MsgErrFieldRequired
        }
    }
    $osValue = Get-Prop $manifest 'os'
    if (@('darwin', 'linux', 'windows') -notcontains $osValue) {
        Invoke-ManifestFieldFailure -Path $manifestPath -Field 'os' -Reason ($script:MsgErrFieldEnum -f $osValue)
    }
    $archValue = Get-Prop $manifest 'arch'
    if (@('arm64', 'x64') -notcontains $archValue) {
        Invoke-ManifestFieldFailure -Path $manifestPath -Field 'arch' -Reason ($script:MsgErrFieldEnum -f $archValue)
    }

    $ca = Get-Prop $manifest 'ca'
    if ($null -eq $ca) {
        Invoke-ManifestFieldFailure -Path $manifestPath -Field 'ca' -Reason $script:MsgErrFieldRequired
    }
    foreach ($field in @('file', 'sha256', 'install_name')) {
        if ([string]::IsNullOrEmpty((Get-Prop $ca $field))) {
            Invoke-ManifestFieldFailure -Path $manifestPath -Field ('ca.' + $field) -Reason $script:MsgErrFieldRequired
        }
    }
    if (-not (Test-Sha256Value (Get-Prop $ca 'sha256'))) {
        Invoke-ManifestFieldFailure -Path $manifestPath -Field 'ca.sha256' -Reason $script:MsgErrFieldSha
    }
    if (-not (Test-PackagePath (Get-Prop $ca 'file'))) {
        Invoke-ManifestFieldFailure -Path $manifestPath -Field 'ca.file' -Reason ($script:MsgErrFieldPath -f (Get-Prop $ca 'file'))
    }
    if (-not (Test-Path -LiteralPath (Join-Path $PackageRoot ((Get-Prop $ca 'file').Replace('/', [string][IO.Path]::DirectorySeparatorChar))) -PathType Leaf)) {
        Invoke-ManifestFieldFailure -Path $manifestPath -Field 'ca.file' -Reason ($script:MsgErrFieldNoFile -f (Get-Prop $ca 'file'))
    }
    if (-not (Test-PackagePath (Get-Prop $ca 'install_name'))) {
        Invoke-ManifestFieldFailure -Path $manifestPath -Field 'ca.install_name' -Reason ($script:MsgErrFieldPath -f (Get-Prop $ca 'install_name'))
    }

    $artifacts = Get-Prop $manifest 'artifacts'
    if ($null -eq $artifacts) {
        Invoke-ManifestFieldFailure -Path $manifestPath -Field 'artifacts' -Reason $script:MsgErrFieldRequired
    }
    $artifactList = @($artifacts)
    $cliCount = 0
    $desktopCount = 0
    for ($i = 0; $i -lt $artifactList.Count; $i++) {
        $item = $artifactList[$i]
        $kind = Get-Prop $item 'kind'
        $file = Get-Prop $item 'file'
        if ([string]::IsNullOrEmpty($kind)) {
            Invoke-ManifestFieldFailure -Path $manifestPath -Field ("artifacts.$i.kind") -Reason $script:MsgErrFieldRequired
        }
        if ([string]::IsNullOrEmpty($file)) {
            Invoke-ManifestFieldFailure -Path $manifestPath -Field ("artifacts.$i.file") -Reason $script:MsgErrFieldRequired
        }
        if (-not (Test-PackagePath $file)) {
            Invoke-ManifestFieldFailure -Path $manifestPath -Field ("artifacts.$i.file") -Reason ($script:MsgErrFieldPath -f $file)
        }
        if (-not (Test-Path -LiteralPath (Join-Path $PackageRoot ($file.Replace('/', [string][IO.Path]::DirectorySeparatorChar))) -PathType Leaf)) {
            Invoke-ManifestFieldFailure -Path $manifestPath -Field ("artifacts.$i.file") -Reason ($script:MsgErrFieldNoFile -f $file)
        }
        if (-not (Test-Sha256Value (Get-Prop $item 'sha256'))) {
            Invoke-ManifestFieldFailure -Path $manifestPath -Field ("artifacts.$i.sha256") -Reason $script:MsgErrFieldSha
        }
        if ($kind -eq 'cli') {
            $cliCount++
            if ([string]::IsNullOrEmpty((Get-Prop $item 'install_name'))) {
                Invoke-ManifestFieldFailure -Path $manifestPath -Field ("artifacts.$i.install_name") -Reason $script:MsgErrFieldRequired
            }
            if (-not (Test-PackagePath (Get-Prop $item 'install_name'))) {
                Invoke-ManifestFieldFailure -Path $manifestPath -Field ("artifacts.$i.install_name") -Reason ($script:MsgErrFieldPath -f (Get-Prop $item 'install_name'))
            }
        } elseif ($kind -eq 'desktop') {
            $desktopCount++
            # app_name проверяется строже пути внутри пакета: одно имя без разделителей,
            # самоссылок и подстановочных символов. Для installer_type="dmg" поле обязательно и
            # непусто — паритет с common/install-posix.sh, где пустое значение означало бы
            # rm -rf каталога /Applications целиком (N5-P4).
            $appName = Get-Prop $item 'app_name'
            if ((Get-Prop $item 'installer_type') -eq 'dmg' -and [string]::IsNullOrWhiteSpace($appName)) {
                Invoke-ManifestFieldFailure -Path $manifestPath -Field ("artifacts.$i.app_name") -Reason $script:MsgErrFieldRequired
            }
            if (-not [string]::IsNullOrEmpty($appName) -and -not (Test-AppName $appName)) {
                Invoke-ManifestFieldFailure -Path $manifestPath -Field ("artifacts.$i.app_name") -Reason ($script:MsgErrFieldPath -f $appName)
            }
        }
    }
    if ($cliCount -ne 1) {
        Invoke-ManifestFieldFailure -Path $manifestPath -Field 'artifacts' -Reason ($script:MsgErrFieldCli -f $cliCount)
    }
    if ($desktopCount -gt 1) {
        Invoke-ManifestFieldFailure -Path $manifestPath -Field 'artifacts' -Reason ($script:MsgErrFieldDesktop -f $desktopCount)
    }
    if ($null -eq (Get-Prop $manifest 'purge_paths')) {
        Invoke-ManifestFieldFailure -Path $manifestPath -Field 'purge_paths' -Reason $script:MsgErrFieldRequired
    }

    $script:ManifestPath = $manifestPath
    return $manifest
}

function Get-Artifact {
    param($Manifest, [string]$Kind)
    foreach ($item in @(Get-Prop $Manifest 'artifacts')) {
        if ((Get-Prop $item 'kind') -eq $Kind) {
            return $item
        }
    }
    return $null
}

function Get-PackageFilePath {
    param([string]$PackageRoot, [string]$RelativePath)
    return (Join-Path $PackageRoot ($RelativePath.Replace('/', [string][IO.Path]::DirectorySeparatorChar)))
}

# =====================================================================================
# Тонкие обёртки над средой — в тестах подменяются моками (N5-T2)
# =====================================================================================
function Test-IsWindowsHost {
    return ($env:OS -eq 'Windows_NT')
}

function Get-HostOsArch {
    if (-not (Test-IsWindowsHost)) {
        return @{ Os = 'unknown'; Arch = 'unknown' }
    }
    $arch = 'unsupported'
    if ($env:PROCESSOR_ARCHITECTURE -eq 'AMD64') {
        $arch = 'x64'
    }
    return @{ Os = 'windows'; Arch = $arch }
}

function Get-FileSha256Hash {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-UserEnvironmentVariable {
    param([Parameter(Mandatory = $true)][string]$Name)
    return [Environment]::GetEnvironmentVariable($Name, 'User')
}

function Write-UserEnvironmentVariable {
    param([Parameter(Mandatory = $true)][string]$Name, [string]$Value)
    [Environment]::SetEnvironmentVariable($Name, $Value, 'User')
}

# Чтение PATH пользователя без развёртывания %…% (N5-I8): иначе запись уничтожит подстановки.
function Get-UserPathEntry {
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $false)
    if ($null -eq $key) {
        return @{ Value = ''; Kind = 'ExpandString' }
    }
    try {
        $value = $key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $kind = 'ExpandString'
        $names = $key.GetValueNames()
        if ($names -contains 'Path') {
            $kind = $key.GetValueKind('Path').ToString()
        }
        if ($null -eq $value) { $value = '' }
        return @{ Value = [string]$value; Kind = $kind }
    } finally {
        $key.Close()
    }
}

function Write-UserPathEntry {
    param([Parameter(Mandatory = $true)][string]$Value, [string]$Kind = 'ExpandString')
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
    try {
        $valueKind = [Microsoft.Win32.RegistryValueKind]::ExpandString
        if ($Kind -eq 'String') {
            $valueKind = [Microsoft.Win32.RegistryValueKind]::String
        }
        $key.SetValue('Path', $Value, $valueKind)
    } finally {
        $key.Close()
    }
}

# Рассылка WM_SETTINGCHANGE: неуспех — предупреждение, а не ошибка (N5-I8).
function Publish-EnvironmentChange {
    if (-not (Test-IsWindowsHost)) {
        return $false
    }
    try {
        if (-not ([System.Management.Automation.PSTypeName]'OpenCodeMagnit.NativeMethods').Type) {
            Add-Type -Namespace 'OpenCodeMagnit' -Name 'NativeMethods' -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true, CharSet = System.Runtime.InteropServices.CharSet.Auto)]
public static extern System.IntPtr SendMessageTimeout(System.IntPtr hWnd, uint Msg, System.UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out System.UIntPtr lpdwResult);
'@
        }
        $result = [System.UIntPtr]::Zero
        $sent = [OpenCodeMagnit.NativeMethods]::SendMessageTimeout(
            [System.IntPtr]0xffff, 0x1A, [System.UIntPtr]::Zero, 'Environment', 0x0002, 5000, [ref]$result)
        return ($sent -ne [System.IntPtr]::Zero)
    } catch {
        return $false
    }
}

function Invoke-DesktopInstaller {
    param([Parameter(Mandatory = $true)][string]$Path, [string[]]$Arguments)
    if ($null -eq $Arguments -or $Arguments.Count -eq 0) {
        $process = Start-Process -FilePath $Path -Wait -PassThru
    } else {
        $process = Start-Process -FilePath $Path -ArgumentList $Arguments -Wait -PassThru
    }
    return $process.ExitCode
}

function Get-InstalledVersion {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return ''
    }
    try {
        $global:LASTEXITCODE = 0
        $output = & $Path --version 2>$null
        if ($LASTEXITCODE -ne 0) {
            return ''
        }
        if ($null -eq $output) {
            return ''
        }
        $first = @($output)[0]
        if ($null -eq $first) {
            return ''
        }
        return ([string]$first).Trim()
    } catch {
        return ''
    }
}

# =====================================================================================
# Проверки среды и целостности (N5-I3, N5-P5)
# =====================================================================================
function Test-EnvironmentCompatible {
    param($Manifest)
    $psMajor = $PSVersionTable.PSVersion.Major
    $psMinor = $PSVersionTable.PSVersion.Minor
    if ($psMajor -lt 5 -or ($psMajor -eq 5 -and $psMinor -lt 1)) {
        Invoke-Failure -Code $script:ExitEnv -Message ($script:MsgErrPsVersion -f $PSVersionTable.PSVersion.ToString())
    }
    $hostInfo = Get-HostOsArch
    if ($hostInfo.Os -ne 'windows' -or $hostInfo.Arch -ne 'x64') {
        Invoke-Failure -Code $script:ExitEnv -Message ($script:MsgErrOsUnsupported -f $hostInfo.Os, $hostInfo.Arch)
    }
    if ((Get-Prop $Manifest 'os') -ne $hostInfo.Os -or (Get-Prop $Manifest 'arch') -ne $hostInfo.Arch) {
        Invoke-Failure -Code $script:ExitEnv -Message ($script:MsgErrOsMismatch -f (Get-Prop $Manifest 'os'), (Get-Prop $Manifest 'arch'), $hostInfo.Os, $hostInfo.Arch)
    }
}

# Обязательная проверка хеша (N5-P5): флага пропуска не существует.
function Confirm-FileHash {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Expected)
    $actual = Get-FileSha256Hash -Path $Path
    $want = $Expected.ToLowerInvariant()
    if ($actual -ne $want) {
        Invoke-Failure -Code $script:ExitHash -Message ($script:MsgErrHashMismatch -f $Path, $want, $actual)
    }
}

function Confirm-PackageIntegrity {
    param($Manifest, [string]$PackageRoot, [bool]$WithDesktop)
    $ca = Get-Prop $Manifest 'ca'
    Confirm-FileHash -Path (Get-PackageFilePath $PackageRoot (Get-Prop $ca 'file')) -Expected (Get-Prop $ca 'sha256')
    $cli = Get-Artifact $Manifest 'cli'
    Confirm-FileHash -Path (Get-PackageFilePath $PackageRoot (Get-Prop $cli 'file')) -Expected (Get-Prop $cli 'sha256')
    $desktop = Get-Artifact $Manifest 'desktop'
    if ($null -ne $desktop -and $WithDesktop) {
        Confirm-FileHash -Path (Get-PackageFilePath $PackageRoot (Get-Prop $desktop 'file')) -Expected (Get-Prop $desktop 'sha256')
    }
}

# =====================================================================================
# Раскладка путей (N5-I4, N5-I8)
# =====================================================================================
function Get-UserProfileDir {
    if (-not [string]::IsNullOrEmpty($env:USERPROFILE)) {
        return $env:USERPROFILE
    }
    # Запасной вариант для прогона тестов на pwsh под macOS/Linux (N5-T2).
    return $env:HOME
}

function Get-LocalAppDataDir {
    if (-not [string]::IsNullOrEmpty($env:LOCALAPPDATA)) {
        return $env:LOCALAPPDATA
    }
    return (Join-Path (Get-UserProfileDir) 'AppData\Local')
}

function Get-Layout {
    param($Manifest, [string]$PackageRoot, [string]$PrefixDir)
    $configDir = Join-Path (Join-Path (Get-UserProfileDir) '.config') 'opencode'
    if ([string]::IsNullOrEmpty($PrefixDir)) {
        $binDir = Join-Path (Join-Path (Get-LocalAppDataDir) 'Programs') 'opencode'
    } else {
        $binDir = $PrefixDir
    }
    $cli = Get-Artifact $Manifest 'cli'
    $ca = Get-Prop $Manifest 'ca'
    $binName = Get-Prop $cli 'install_name'
    return @{
        ConfigDir  = $configDir
        BinDir     = $binDir
        BinTarget  = (Join-Path $binDir $binName)
        BinBackup  = (Join-Path $binDir ($binName + '.bak'))
        CaTarget   = (Join-Path $configDir (Get-Prop $ca 'install_name'))
        CaSource   = (Get-PackageFilePath $PackageRoot (Get-Prop $ca 'file'))
        CaSha      = ([string](Get-Prop $ca 'sha256')).ToLowerInvariant()
        CliSource  = (Get-PackageFilePath $PackageRoot (Get-Prop $cli 'file'))
        CliSha     = ([string](Get-Prop $cli 'sha256')).ToLowerInvariant()
        ConfigFile = (Join-Path $configDir 'opencode.json')
        ConfigBak  = (Join-Path $configDir 'opencode.json.bak')
    }
}

function Test-UserPathContains {
    param([string]$PathValue, [string]$Directory)
    $needle = $Directory.TrimEnd('\').ToLowerInvariant()
    foreach ($part in $PathValue.Split(';')) {
        if ([string]::IsNullOrEmpty($part)) { continue }
        if ($part.TrimEnd('\').ToLowerInvariant() -eq $needle) {
            return $true
        }
    }
    return $false
}

# =====================================================================================
# Шаги установки
# =====================================================================================
function Invoke-CaInstall {
    param($Layout)
    if (-not (Test-Path -LiteralPath $Layout.ConfigDir -PathType Container)) {
        New-Item -ItemType Directory -Path $Layout.ConfigDir -Force | Out-Null
    }
    if (Test-Path -LiteralPath $Layout.CaTarget -PathType Leaf) {
        if ((Get-FileSha256Hash -Path $Layout.CaTarget) -eq $Layout.CaSha) {
            Write-Say $script:MsgCaSame
            return
        }
        Copy-Item -LiteralPath $Layout.CaSource -Destination $Layout.CaTarget -Force
        Write-Say ($script:MsgCaReplaced -f $Layout.CaTarget)
        return
    }
    Copy-Item -LiteralPath $Layout.CaSource -Destination $Layout.CaTarget -Force
    Write-Say ($script:MsgCaInstalled -f $Layout.CaTarget)
}

function Invoke-BinaryInstall {
    param($Layout, [string]$PackageVersion)
    if (-not (Test-Path -LiteralPath $Layout.BinDir -PathType Container)) {
        try {
            New-Item -ItemType Directory -Path $Layout.BinDir -Force | Out-Null
        } catch {
            Invoke-Failure -Code $script:ExitPerm -Message ($script:MsgErrNoWrite -f $Layout.BinDir)
        }
    }
    if (Test-Path -LiteralPath $Layout.BinTarget -PathType Leaf) {
        if ((Get-FileSha256Hash -Path $Layout.BinTarget) -eq $Layout.CliSha) {
            Write-Say ($script:MsgBinSame -f $PackageVersion)
            return
        }
        $current = Get-InstalledVersion -Path $Layout.BinTarget
        if ([string]::IsNullOrEmpty($current)) {
            $current = 'не определена'
        }
        Write-Say ($script:MsgBinOther -f $current, $PackageVersion)
        # Windows позволяет переименовать запущенный exe; неуспех — файл занят (N5-I8), код 6.
        if (Test-Path -LiteralPath $Layout.BinBackup -PathType Leaf) {
            Remove-Item -LiteralPath $Layout.BinBackup -Force
        }
        try {
            Move-Item -LiteralPath $Layout.BinTarget -Destination $Layout.BinBackup -Force
        } catch {
            Invoke-Failure -Code $script:ExitBusy -Message $script:MsgErrBusy
        }
        Write-Say ($script:MsgBinBackup -f $Layout.BinBackup)
    }
    try {
        Copy-Item -LiteralPath $Layout.CliSource -Destination $Layout.BinTarget -Force
    } catch {
        Invoke-Failure -Code $script:ExitPerm -Message ($script:MsgErrNoWrite -f $Layout.BinDir)
    }
    Write-Say ($script:MsgBinInstalled -f $Layout.BinTarget)
}

function Invoke-EnvironmentSetup {
    param($Layout)
    $current = Get-UserEnvironmentVariable -Name $script:EnvName
    if ($current -eq $Layout.CaTarget) {
        Write-Say $script:MsgEnvSame
    } else {
        Write-UserEnvironmentVariable -Name $script:EnvName -Value $Layout.CaTarget
        Write-Say ($script:MsgEnvSet -f $Layout.CaTarget)
    }

    $entry = Get-UserPathEntry
    if (Test-UserPathContains -PathValue $entry.Value -Directory $Layout.BinDir) {
        Write-Say ($script:MsgPathSame -f $Layout.BinDir)
        return $false
    }
    $newValue = $entry.Value
    if ([string]::IsNullOrEmpty($newValue)) {
        $newValue = $Layout.BinDir
    } else {
        $newValue = $newValue.TrimEnd(';') + ';' + $Layout.BinDir
    }
    Write-UserPathEntry -Value $newValue -Kind $entry.Kind
    Write-Say ($script:MsgPathAdded -f $Layout.BinDir)
    if (-not (Publish-EnvironmentChange)) {
        Write-Say $script:MsgPathWarn
    }
    return $true
}

function Invoke-DesktopStep {
    param($Manifest, [string]$PackageRoot, [bool]$SkipDesktop)
    $desktop = Get-Artifact $Manifest 'desktop'
    if ($null -eq $desktop) {
        Write-Say $script:MsgDesktopNone
        return @{ Summary = 'не входит в пакет'; Failed = $false }
    }
    if ($SkipDesktop) {
        Write-Say $script:MsgDesktopSkipped
        return @{ Summary = 'пропущен (--no-desktop)'; Failed = $false }
    }
    $installerPath = Get-PackageFilePath $PackageRoot (Get-Prop $desktop 'file')
    $silent = Get-Prop $desktop 'silent_args'
    if ($null -eq $silent -or @($silent).Count -eq 0) {
        # Ключи тихой установки не угадываются (N5-I10).
        Write-Say ($script:MsgDesktopManualAt -f $installerPath)
        return @{ Summary = $installerPath; Failed = $false }
    }
    $code = Invoke-DesktopInstaller -Path $installerPath -Arguments @($silent)
    if ($code -ne 0) {
        Write-Say ($script:MsgDesktopError -f $code)
        return @{ Summary = ('ошибка (код {0})' -f $code); Failed = $true }
    }
    Write-Say ($script:MsgDesktopInstalled -f $installerPath)
    return @{ Summary = $installerPath; Failed = $false }
}

# Конфликт конфига = сам факт существования файла; JSON не разбирается (N5-I11).
function Invoke-UserConfigStep {
    param($Layout)
    if (Test-Path -LiteralPath $Layout.ConfigFile -PathType Leaf) {
        if (Test-Path -LiteralPath $Layout.ConfigBak -PathType Leaf) {
            Write-Say ($script:MsgConfigBackupKept -f $Layout.ConfigBak)
        } else {
            Copy-Item -LiteralPath $Layout.ConfigFile -Destination $Layout.ConfigBak -Force
            Write-Say ($script:MsgConfigBackup -f $Layout.ConfigBak)
        }
    }
    Write-Say ($script:MsgConfigKept -f $Layout.ConfigFile)
}

function Write-InstallReport {
    param($Manifest, $Layout, [string]$DesktopSummary, [bool]$PathChanged)
    Write-Line ($script:MsgDone -f (Get-Prop $Manifest 'version'))
    Write-Line ($script:MsgSumBin -f $Layout.BinTarget)
    Write-Line ($script:MsgSumCa -f $Layout.CaTarget)
    Write-Line ($script:MsgSumDesktop -f $DesktopSummary)
    Write-Line ($script:MsgSumConfig -f $Layout.ConfigFile)
    Write-Line ($script:MsgSumHub -f (Get-Prop $Manifest 'hub_url'))
    Write-Line $script:MsgNext
    if ($PathChanged) {
        Write-Line $script:MsgNextTerminal
    }
    Write-Line $script:MsgNextRun
    Write-Line $script:MsgNextSso
}

function Invoke-Install {
    param($Manifest, $Layout, [string]$PackageRoot, [bool]$SkipDesktop, [bool]$SkipLaunch)
    Invoke-CaInstall -Layout $Layout
    Invoke-BinaryInstall -Layout $Layout -PackageVersion (Get-Prop $Manifest 'version')
    $pathChanged = Invoke-EnvironmentSetup -Layout $Layout
    $desktop = Invoke-DesktopStep -Manifest $Manifest -PackageRoot $PackageRoot -SkipDesktop $SkipDesktop
    Invoke-UserConfigStep -Layout $Layout
    Write-InstallReport -Manifest $Manifest -Layout $Layout -DesktopSummary $desktop.Summary -PathChanged $pathChanged
    if (-not $SkipLaunch) {
        try {
            # Код возврата corp status игнорируется: сразу после установки ключа ещё нет (N5-I12).
            $statusOutput = & $Layout.BinTarget corp status 2>&1
            foreach ($statusLine in @($statusOutput)) {
                Write-Line ([string]$statusLine)
            }
        } catch {
            Write-Say $_.Exception.Message
        }
    }
    if ($desktop.Failed) {
        return $script:ExitFail
    }
    return $script:ExitOk
}

# =====================================================================================
# --check (N5-C2, N5-C3)
# =====================================================================================
function Invoke-Check {
    param($Manifest, $Layout)
    $diff = $false
    Write-Line ($script:MsgCheckPkg -f (Get-Prop $Manifest 'version'), (Get-Prop $Manifest 'os'), (Get-Prop $Manifest 'arch'))
    Write-Line $script:MsgCheckManifest

    if (Test-Path -LiteralPath $Layout.BinTarget -PathType Leaf) {
        $hash = Get-FileSha256Hash -Path $Layout.BinTarget
        $version = Get-InstalledVersion -Path $Layout.BinTarget
        if ($hash -eq $Layout.CliSha) {
            if ([string]::IsNullOrEmpty($version)) {
                Write-Line ($script:MsgCheckBinNoVer -f $Layout.BinTarget)
            } else {
                Write-Line ($script:MsgCheckBinOk -f $version, $Layout.BinTarget)
            }
        } elseif ([string]::IsNullOrEmpty($version)) {
            Write-Line ($script:MsgCheckBinNoVer -f $Layout.BinTarget)
            $diff = $true
        } else {
            Write-Line ($script:MsgCheckBinOther -f $version, (Get-Prop $Manifest 'version'))
            $diff = $true
        }
    } else {
        Write-Line $script:MsgCheckBinNone
        $diff = $true
    }

    $entry = Get-UserPathEntry
    if (Test-UserPathContains -PathValue $entry.Value -Directory $Layout.BinDir) {
        Write-Line ($script:MsgCheckPathYes -f $Layout.BinDir)
    } else {
        Write-Line ($script:MsgCheckPathNo -f $Layout.BinDir)
        $diff = $true
    }

    if (Test-Path -LiteralPath $Layout.CaTarget -PathType Leaf) {
        if ((Get-FileSha256Hash -Path $Layout.CaTarget) -eq $Layout.CaSha) {
            Write-Line ($script:MsgCheckCaOk -f $Layout.CaTarget)
        } else {
            Write-Line ($script:MsgCheckCaDiff -f $Layout.CaTarget)
            $diff = $true
        }
    } else {
        Write-Line $script:MsgCheckCaNone
        $diff = $true
    }

    $envValue = Get-UserEnvironmentVariable -Name $script:EnvName
    if ([string]::IsNullOrEmpty($envValue)) {
        Write-Line $script:MsgCheckEnvNone
        $diff = $true
    } elseif ($envValue -eq $Layout.CaTarget) {
        Write-Line ($script:MsgCheckEnvOk -f $Layout.CaTarget)
    } else {
        Write-Line ($script:MsgCheckEnvOther -f $envValue)
        $diff = $true
    }

    $desktop = Get-Artifact $Manifest 'desktop'
    if ($null -eq $desktop) {
        Write-Line $script:MsgDesktopNone
    } else {
        $registered = Get-DesktopUninstallEntry -Manifest $Manifest
        if ($null -ne $registered) {
            Write-Line ($script:MsgCheckDesktopOk -f $registered.DisplayTarget)
        } else {
            Write-Line $script:MsgCheckDesktopNone
            $diff = $true
        }
    }

    Write-Line ($script:MsgCheckConfig -f $Layout.ConfigFile)
    Write-Line ($script:MsgCheckHub -f (Get-Prop $Manifest 'hub_url'))
    if ($diff) {
        Write-Line $script:MsgCheckSumNeed
        return $script:ExitDiff
    }
    Write-Line $script:MsgCheckSumOk
    return $script:ExitOk
}

# Штатный деинсталлятор Desktop, если он зарегистрирован в системе (N5-R1).
function Get-DesktopUninstallEntry {
    param($Manifest)
    if (-not (Test-IsWindowsHost)) {
        return $null
    }
    $desktop = Get-Artifact $Manifest 'desktop'
    if ($null -eq $desktop) {
        return $null
    }
    $appName = Get-Prop $desktop 'app_name'
    if ([string]::IsNullOrWhiteSpace($appName)) {
        $appName = 'OpenCode'
    }
    $needle = $appName.Replace('.app', '')
    if ([string]::IsNullOrWhiteSpace($needle)) {
        return $null
    }
    # Подстановочные символы в app_name экранируются: иначе значение вида "*" совпало бы с ПЕРВОЙ
    # записью Uninstall, и её UninstallString был бы запущен со silent_args из манифеста (N5-R1).
    $pattern = '*' + [System.Management.Automation.WildcardPattern]::Escape($needle) + '*'
    $roots = @(
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall'
    )
    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        foreach ($item in Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue) {
            $props = Get-ItemProperty -LiteralPath $item.PSPath -ErrorAction SilentlyContinue
            $displayName = Get-Prop $props 'DisplayName'
            if ([string]::IsNullOrEmpty($displayName)) { continue }
            if ($displayName -like $pattern) {
                $installLocation = Get-Prop $props 'InstallLocation'
                # Для строк контракта (--check «путь», отчёт удаления) предпочитаем путь установки,
                # если он в реестре есть; иначе — отображаемое имя. Значение одно и то же в обоих
                # местах, чтобы вывод был консистентным.
                $displayTarget = $displayName
                if (-not [string]::IsNullOrEmpty($installLocation)) {
                    $displayTarget = $installLocation
                }
                return @{
                    DisplayName          = $displayName
                    DisplayTarget        = $displayTarget
                    UninstallString      = (Get-Prop $props 'UninstallString')
                    QuietUninstallString = (Get-Prop $props 'QuietUninstallString')
                    InstallLocation      = $installLocation
                }
            }
        }
    }
    return $null
}

# Разбор строки деинсталляции реестра на исполняемый файл и аргументы (N5-R1).
# UninstallString бывает вида "C:\...\uninstall.exe" /S, MsiExec.exe /X{GUID} либо
# C:\Program Files\App\unins000.exe /S — некавыченный путь с пробелами. В последнем случае
# граница определяется по расширению исполняемого файла; если однозначной границы нет,
# возвращается $null и деинсталлятор НЕ запускается (лучше «удалите вручную», чем чужой процесс).
function ConvertFrom-UninstallString {
    param([Parameter(Mandatory = $true)][string]$Command)
    $trimmed = $Command.Trim()
    $path = $trimmed
    $rest = ''
    if ($trimmed.StartsWith('"')) {
        $end = $trimmed.IndexOf('"', 1)
        if ($end -gt 0) {
            $path = $trimmed.Substring(1, $end - 1)
            $rest = $trimmed.Substring($end + 1).Trim()
        } else {
            $path = $trimmed.Trim('"')
        }
    } else {
        $exeMatch = [regex]::Match($trimmed, '^(.*?\.(?:exe|com|bat|cmd))(\s|$)', 'IgnoreCase')
        if ($exeMatch.Success) {
            $path = $exeMatch.Groups[1].Value
            $rest = $trimmed.Substring($path.Length).Trim()
        } elseif ($trimmed.IndexOf(' ') -gt 0) {
            # Пробел есть, а границы пути нет: разбор неоднозначен.
            return $null
        }
    }
    if ([string]::IsNullOrWhiteSpace($path)) {
        return $null
    }
    $arguments = @()
    if (-not [string]::IsNullOrEmpty($rest)) {
        # Аргумент может содержать пробел внутри кавычек: /D="C:\Program Files\App" — один аргумент.
        $arguments = @([regex]::Matches($rest, '(?:"[^"]*"|[^\s"])+') | ForEach-Object { $_.Value })
    }
    return @{ Path = $path; Arguments = $arguments }
}

# Запуск штатного деинсталлятора и ожидание завершения; в тестах подменяется моком (N5-T2).
function Invoke-UninstallProcess {
    param([Parameter(Mandatory = $true)][string]$Path, [string[]]$Arguments)
    if ($null -eq $Arguments -or $Arguments.Count -eq 0) {
        $process = Start-Process -FilePath $Path -Wait -PassThru
    } else {
        $process = Start-Process -FilePath $Path -ArgumentList $Arguments -Wait -PassThru
    }
    return $process.ExitCode
}

# =====================================================================================
# --dry-run (N5-C1)
# =====================================================================================
function Get-InstallPlan {
    param($Manifest, $Layout, [string]$PackageRoot, [bool]$SkipDesktop, [bool]$SkipLaunch)
    $lines = New-Object System.Collections.ArrayList
    [void]$lines.Add($script:MsgPlanHead -f (Get-Prop $Manifest 'version'), (Get-Prop $Manifest 'os'), (Get-Prop $Manifest 'arch'))
    $n = 0
    if (-not (Test-Path -LiteralPath $Layout.ConfigDir -PathType Container)) {
        $n++
        [void]$lines.Add($script:MsgPlanStep -f $n, ($script:MsgPlanMkdir -f $Layout.ConfigDir))
    }
    $n++
    [void]$lines.Add($script:MsgPlanStep -f $n, ($script:MsgPlanCa -f $Layout.CaSource, $Layout.CaTarget))
    if (-not (Test-Path -LiteralPath $Layout.BinDir -PathType Container)) {
        $n++
        [void]$lines.Add($script:MsgPlanStep -f $n, ($script:MsgPlanMkdir -f $Layout.BinDir))
    }
    if (Test-Path -LiteralPath $Layout.BinTarget -PathType Leaf) {
        $n++
        [void]$lines.Add($script:MsgPlanStep -f $n, ($script:MsgPlanBinBackup -f $Layout.BinTarget, $Layout.BinBackup))
    }
    $n++
    [void]$lines.Add($script:MsgPlanStep -f $n, ($script:MsgPlanBin -f $Layout.CliSource, $Layout.BinTarget))
    $n++
    [void]$lines.Add($script:MsgPlanStep -f $n, ($script:MsgPlanEnv -f $script:EnvName, $Layout.CaTarget))
    $n++
    [void]$lines.Add($script:MsgPlanStep -f $n, ($script:MsgPlanPath -f $Layout.BinDir))

    $desktop = Get-Artifact $Manifest 'desktop'
    $n++
    if ($null -eq $desktop) {
        [void]$lines.Add($script:MsgPlanStep -f $n, $script:MsgDesktopNone)
    } elseif ($SkipDesktop) {
        [void]$lines.Add($script:MsgPlanStep -f $n, $script:MsgDesktopSkipped)
    } else {
        $installerPath = Get-PackageFilePath $PackageRoot (Get-Prop $desktop 'file')
        $silent = Get-Prop $desktop 'silent_args'
        if ($null -eq $silent -or @($silent).Count -eq 0) {
            [void]$lines.Add($script:MsgPlanStep -f $n, ($script:MsgDesktopManualAt -f $installerPath))
        } else {
            [void]$lines.Add($script:MsgPlanStep -f $n, ($script:MsgPlanDesktopRun -f $installerPath, ([string]::Join(' ', @($silent)))))
        }
    }
    $n++
    [void]$lines.Add($script:MsgPlanStep -f $n, ($script:MsgPlanConfig -f $Layout.ConfigFile, $Layout.ConfigBak))
    if (-not $SkipLaunch) {
        $n++
        [void]$lines.Add($script:MsgPlanStep -f $n, ($script:MsgPlanCorpStatus -f $Layout.BinTarget))
    }
    [void]$lines.Add($script:MsgCheckHub -f (Get-Prop $Manifest 'hub_url'))
    return $lines.ToArray()
}

function Get-UninstallPlan {
    param($Manifest, $Layout, [bool]$WithPurge)
    $lines = New-Object System.Collections.ArrayList
    [void]$lines.Add($script:MsgPlanUninstallHead -f (Get-Prop $Manifest 'version'))
    $n = 0
    foreach ($item in @($Layout.BinTarget, $Layout.BinBackup)) {
        $n++
        [void]$lines.Add($script:MsgPlanStep -f $n, ($script:MsgPlanRemove -f $item))
    }
    $n++
    [void]$lines.Add($script:MsgPlanStep -f $n, ($script:MsgPlanPathRemove -f $Layout.BinDir))
    $n++
    [void]$lines.Add($script:MsgPlanStep -f $n, ($script:MsgPlanEnvUnset -f $script:EnvName))
    $n++
    [void]$lines.Add($script:MsgPlanStep -f $n, ($script:MsgPlanRemove -f $Layout.CaTarget))
    if ($WithPurge) {
        [void]$lines.Add($script:MsgPurgeHead)
        foreach ($path in (Get-PurgePathList -Manifest $Manifest)) {
            [void]$lines.Add($script:MsgPurgeItem -f $path)
        }
    }
    return $lines.ToArray()
}

# =====================================================================================
# Удаление (N5-R1…N5-R4)
# =====================================================================================
function Expand-UserPathTemplate {
    param([string]$Value)
    $result = $Value
    $userProfile = Get-UserProfileDir
    $localAppData = Get-LocalAppDataDir
    $result = $result.Replace('%USERPROFILE%', $userProfile)
    $result = $result.Replace('%LOCALAPPDATA%', $localAppData)
    $result = $result.Replace('${XDG_CONFIG_HOME}', (Join-Path $userProfile '.config'))
    $result = $result.Replace('${XDG_DATA_HOME}', (Join-Path (Join-Path $userProfile '.local') 'share'))
    $result = $result.Replace('${HOME}', $userProfile)
    if ($result -eq '~') {
        $result = $userProfile
    } elseif ($result.StartsWith('~/') -or $result.StartsWith('~\')) {
        $result = Join-Path $userProfile $result.Substring(2)
    }
    return $result
}

# Единая нормализация пути перед любой файловой операцией — паритет с path_normalize из
# install-posix.sh (N5-P3, N5-R2). Строковое сравнение с профилем пользователя обходится любой
# формой, которую файловая система схлопывает сама: '%USERPROFILE%\', '%USERPROFILE%\\',
# '%USERPROFILE%\.', '%USERPROFILE%\.\', '%USERPROFILE%\sub\..' — всё это тот же каталог.
# GetFullPath приводит их к одному значению по правилам самой ФС; сегменты '..' отвергаются
# отдельно (лексический «подъём» маскирует намерение), относительный путь тоже отвергается —
# достраивать его от текущего каталога процесса здесь недопустимо.
function ConvertTo-NormalPath {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    foreach ($segment in $Value.Split([char[]]@('\', '/'))) {
        if ($segment -eq '..') { return $null }
    }
    if (-not [System.IO.Path]::IsPathRooted($Value)) { return $null }
    $full = $null
    try {
        $full = [System.IO.Path]::GetFullPath($Value)
    } catch {
        return $null
    }
    if ([string]::IsNullOrWhiteSpace($full)) { return $null }
    # Оба разделителя: на Windows GetFullPath приводит '/' к '\', но Pester-набор гоняется и на
    # pwsh под macOS/Linux (N5-T2), где приведения не происходит.
    $trimmed = $full.TrimEnd([char[]]@('\', '/'))
    # Корень тома ('C:\', '/') после TrimEnd пуст — возвращаем как есть, вложенным он не станет.
    if ([string]::IsNullOrEmpty($trimmed)) { return $full }
    return $trimmed
}

# Строгая вложенность нормализованных путей: Child обязан лежать НИЖЕ Parent — не совпадать с ним
# и не быть его родителем. Паритет с path_is_inside.
function Test-PathInside {
    param([string]$Parent, [string]$Child)
    if ([string]::IsNullOrEmpty($Parent) -or [string]::IsNullOrEmpty($Child)) { return $false }
    $p = $Parent.ToLowerInvariant()
    $c = $Child.ToLowerInvariant()
    if ($c -eq $p) { return $false }
    $trimmed = $p.TrimEnd([char[]]@('\', '/'))
    if ([string]::IsNullOrEmpty($trimmed)) {
        # Parent — корень тома: ниже него лежит любой более длинный путь с тем же началом.
        return ($c.Length -gt $p.Length -and $c.StartsWith($p))
    }
    if ($c -eq $trimmed) { return $false }
    if (-not $c.StartsWith($trimmed)) { return $false }
    if ($c.Length -le ($trimmed.Length + 1)) { return $false }
    $next = $c.Substring($trimmed.Length, 1)
    if ($next -ne '\' -and $next -ne '/') { return $false }
    return $true
}

# N5-R2: удалению подлежат только объекты СТРОГО внутри профиля пользователя. Решение
# принимается по НОРМАЛИЗОВАННОМУ значению, поэтому '%USERPROFILE%' во всех эквивалентных
# формах (завершающие слэши, '.', '.\', 'sub\..') отвергается наравне с путями вне профиля.
function Test-PurgePathSafe {
    param([string]$Raw, [string]$Expanded)
    if ([string]::IsNullOrEmpty($Raw)) { return $false }
    if ($Raw.Contains('..')) { return $false }
    if ([string]::IsNullOrEmpty($Expanded)) { return $false }
    if ($Expanded.Contains('..')) { return $false }
    $candidate = ConvertTo-NormalPath $Expanded
    if ($null -eq $candidate) { return $false }
    $profileDir = ConvertTo-NormalPath (Get-UserProfileDir)
    if ($null -eq $profileDir) { return $false }
    return (Test-PathInside -Parent $profileDir -Child $candidate)
}

# Список для показа (план удаления): печатается ровно то значение, которое пошло бы в
# Remove-Item, то есть нормализованное. Непригодный путь показывается как есть — до удаления
# дело не дойдёт, его отвергнет Confirm-PurgePathList.
function Get-PurgePathList {
    param($Manifest)
    $result = New-Object System.Collections.ArrayList
    foreach ($raw in @(Get-Prop $Manifest 'purge_paths')) {
        $expanded = Expand-UserPathTemplate -Value ([string]$raw)
        $normalized = ConvertTo-NormalPath $expanded
        if ($null -eq $normalized) { $normalized = $expanded }
        [void]$result.Add($normalized)
    }
    return $result.ToArray()
}

# Список проверяется целиком до любого удаления: одна небезопасная запись отвергает список (N5-R2).
function Confirm-PurgePathList {
    param($Manifest)
    $bad = $false
    foreach ($raw in @(Get-Prop $Manifest 'purge_paths')) {
        $expanded = Expand-UserPathTemplate -Value ([string]$raw)
        if (-not (Test-PurgePathSafe -Raw ([string]$raw) -Expanded $expanded)) {
            Write-ErrorLine ($script:MsgErrPurgeUnsafe -f $raw)
            $bad = $true
        }
    }
    if ($bad) {
        Invoke-Failure -Code $script:ExitArgs -Message $script:MsgErrPurgeRejected
    }
}

function Invoke-ObjectRemoval {
    param([string]$Label, [string]$Path)
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
        Write-Say ($script:MsgUninstallRemoved -f $Label, $Path)
    } else {
        Write-Say ($script:MsgUninstallAbsent -f $Label)
    }
}

function Invoke-Uninstall {
    param($Manifest, $Layout, [bool]$WithPurge)
    $desktopFailed = $false
    Write-Say ($script:MsgUninstallHead -f (Get-Prop $Manifest 'version'))
    Invoke-ObjectRemoval -Label $script:ObjBin -Path $Layout.BinTarget
    Invoke-ObjectRemoval -Label $script:ObjBinBak -Path $Layout.BinBackup

    $entry = Get-UserPathEntry
    if (Test-UserPathContains -PathValue $entry.Value -Directory $Layout.BinDir) {
        $kept = New-Object System.Collections.ArrayList
        foreach ($part in $entry.Value.Split(';')) {
            if ([string]::IsNullOrEmpty($part)) { continue }
            if ($part.TrimEnd('\').ToLowerInvariant() -eq $Layout.BinDir.TrimEnd('\').ToLowerInvariant()) { continue }
            [void]$kept.Add($part)
        }
        Write-UserPathEntry -Value ([string]::Join(';', $kept.ToArray())) -Kind $entry.Kind
        [void](Publish-EnvironmentChange)
        Write-Say ($script:MsgUninstallRemoved -f $script:ObjPath, $Layout.BinDir)
    } else {
        Write-Say ($script:MsgUninstallAbsent -f $script:ObjPath)
    }

    $envValue = Get-UserEnvironmentVariable -Name $script:EnvName
    if ($envValue -eq $Layout.CaTarget) {
        Write-UserEnvironmentVariable -Name $script:EnvName -Value $null
        Write-Say ($script:MsgUninstallRemoved -f $script:EnvName, $Layout.CaTarget)
    } elseif ([string]::IsNullOrEmpty($envValue)) {
        Write-Say ($script:MsgUninstallAbsent -f $script:EnvName)
    } else {
        Write-Say $script:MsgUninstallForeignEnv
    }

    Invoke-ObjectRemoval -Label $script:ObjCa -Path $Layout.CaTarget

    $desktop = Get-Artifact $Manifest 'desktop'
    if ($null -eq $desktop) {
        Write-Say $script:MsgDesktopNone
    } else {
        # N5-R1: если штатный деинсталлятор зарегистрирован — запускаем его (тихо, если у нас есть
        # QuietUninstallString или silent_args в манифесте), ждём и отражаем фактический результат.
        # Если деинсталлятора нет — честное сообщение «удалите вручную», без ложного «удалён».
        $registered = Get-DesktopUninstallEntry -Manifest $Manifest
        $command = ''
        $isQuiet = $false
        if ($null -ne $registered) {
            if (-not [string]::IsNullOrEmpty($registered.QuietUninstallString)) {
                $command = $registered.QuietUninstallString
                $isQuiet = $true
            } elseif (-not [string]::IsNullOrEmpty($registered.UninstallString)) {
                $command = $registered.UninstallString
            }
        }
        if ([string]::IsNullOrEmpty($command)) {
            Write-Say $script:MsgDesktopManualRemove
        } else {
            $parsed = ConvertFrom-UninstallString -Command $command
            if ($null -eq $parsed) {
                # Разбор неоднозначен — не запускаем ничего и не печатаем «удалён» (N5-R1).
                Write-Say ($script:MsgDesktopParseFail -f $command)
                Write-Say $script:MsgDesktopManualRemove
                $command = ''
            }
        }
        if (-not [string]::IsNullOrEmpty($command)) {
            $arguments = @($parsed.Arguments)
            if (-not $isQuiet) {
                $silent = Get-Prop $desktop 'silent_args'
                if ($null -ne $silent -and @($silent).Count -gt 0) {
                    $arguments = @($arguments) + @($silent)
                }
            }
            $code = Invoke-UninstallProcess -Path $parsed.Path -Arguments $arguments
            if ($code -eq 0) {
                Write-Say ($script:MsgUninstallRemoved -f $script:ObjDesktop, $registered.DisplayTarget)
            } else {
                Write-Say ($script:MsgDesktopError -f $code)
                $desktopFailed = $true
            }
        }
    }

    if ($WithPurge) {
        Write-Say $script:MsgPurgeHead
        # Вторая, независимая от Confirm-PurgePathList линия защиты (defense in depth): ни один
        # путь не попадает в Remove-Item без повторной проверки прямо на месте удаления.
        foreach ($rawPath in @(Get-Prop $Manifest 'purge_paths')) {
            $rawText = [string]$rawPath
            $expanded = Expand-UserPathTemplate -Value $rawText
            if (-not (Test-PurgePathSafe -Raw $rawText -Expanded $expanded)) {
                Write-ErrorLine ($script:MsgErrPurgeUnsafe -f $rawText)
                Invoke-Failure -Code $script:ExitArgs -Message $script:MsgErrPurgeRejected
            }
            $path = ConvertTo-NormalPath $expanded
            Write-Line ($script:MsgPurgeItem -f $path)
            if (Test-Path -LiteralPath $path) {
                Remove-Item -LiteralPath $path -Recurse -Force
                Write-Say ($script:MsgPurgeRemoved -f $path)
            } else {
                Write-Say ($script:MsgPurgeAbsent -f $path)
            }
        }
    }
    Write-Line $script:MsgUninstallDone
    # Провал штатного деинсталлятора Desktop отражается кодом выхода (как N5-I10 для установки):
    # удаление CLI/CA уже выполнено и не откатывается.
    if ($desktopFailed) {
        return $script:ExitFail
    }
    return $script:ExitOk
}

# =====================================================================================
# Точка входа
# =====================================================================================
function Get-PackageRoot {
    if (-not [string]::IsNullOrEmpty($script:PackageRootOverride)) {
        return $script:PackageRootOverride
    }
    return (Split-Path -Parent $PSCommandPath)
}

function Invoke-Main {
    [CmdletBinding()]
    param(
        [switch]$DryRun,
        [switch]$Check,
        [switch]$Uninstall,
        [switch]$Purge,
        [string]$Prefix,
        [switch]$NoDesktop,
        [switch]$NoLaunch,
        [switch]$Quiet,
        [switch]$Help,
        [switch]$ShowVersion,
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Rest
    )
    $ErrorActionPreference = 'Stop'
    $script:OptQuiet = [bool]$Quiet
    try {
        if ($Help) {
            Write-Line $script:HelpText
            return $script:ExitOk
        }
        if ($null -ne $Rest -and $Rest.Count -gt 0) {
            Write-ErrorLine $script:HelpText
            Invoke-Failure -Code $script:ExitArgs -Message ($script:MsgErrUnknownArg -f $Rest[0])
        }
        if ($DryRun -and $Check) {
            Invoke-Failure -Code $script:ExitArgs -Message ($script:MsgErrExclusive -f '-DryRun, -Check')
        }
        if ($Check -and $Uninstall) {
            Invoke-Failure -Code $script:ExitArgs -Message ($script:MsgErrExclusive -f '-Check, -Uninstall')
        }
        if ($Purge -and -not $Uninstall) {
            Invoke-Failure -Code $script:ExitArgs -Message $script:MsgErrPurgeAlone
        }

        $packageRoot = Get-PackageRoot
        $manifest = Read-Manifest -PackageRoot $packageRoot
        if ($ShowVersion) {
            Write-Line ($script:MsgVersionLine -f (Get-Prop $manifest 'version'), (Get-Prop $manifest 'os'), (Get-Prop $manifest 'arch'))
            return $script:ExitOk
        }
        Test-EnvironmentCompatible -Manifest $manifest
        $layout = Get-Layout -Manifest $manifest -PackageRoot $packageRoot -PrefixDir $Prefix

        if ($Uninstall) {
            if ($Purge) {
                Confirm-PurgePathList -Manifest $manifest
            }
            if ($DryRun) {
                foreach ($line in (Get-UninstallPlan -Manifest $manifest -Layout $layout -WithPurge ([bool]$Purge))) {
                    Write-Line $line
                }
                return $script:ExitOk
            }
            return (Invoke-Uninstall -Manifest $manifest -Layout $layout -WithPurge ([bool]$Purge))
        }

        Confirm-PackageIntegrity -Manifest $manifest -PackageRoot $packageRoot -WithDesktop (-not $NoDesktop)

        if ($Check) {
            return (Invoke-Check -Manifest $manifest -Layout $layout)
        }
        if ($DryRun) {
            foreach ($line in (Get-InstallPlan -Manifest $manifest -Layout $layout -PackageRoot $packageRoot -SkipDesktop ([bool]$NoDesktop) -SkipLaunch ([bool]$NoLaunch))) {
                Write-Line $line
            }
            return $script:ExitOk
        }
        return (Invoke-Install -Manifest $manifest -Layout $layout -PackageRoot $packageRoot -SkipDesktop ([bool]$NoDesktop) -SkipLaunch ([bool]$NoLaunch))
    } catch {
        $code = $script:ExitFail
        $data = $_.Exception.Data
        if ($null -ne $data -and $data.Contains('Code')) {
            $code = [int]$data['Code']
        }
        if (-not [string]::IsNullOrEmpty($_.Exception.Message)) {
            Write-ErrorLine $_.Exception.Message
        }
        return $code
    }
}

# Исполняемый вход: при dot-source (InvocationName = '.') не срабатывает (N5-T2).
if ($MyInvocation.InvocationName -ne '.') {
    exit (Invoke-Main @PSBoundParameters)
}
