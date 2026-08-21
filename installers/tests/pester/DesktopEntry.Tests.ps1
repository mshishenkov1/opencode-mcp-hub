#Requires -Version 5.1
#
# installers/tests/pester/DesktopEntry.Tests.ps1 — поиск и запуск штатного деинсталлятора Desktop
# на Windows: сопоставление записи Uninstall с app_name (N5-R1, N5-P4) и разбор UninstallString.
#
# Прогон: Invoke-Pester installers/tests/pester (задание `pester` в installers/ci/installers.yml),
# pwsh на ubuntu-latest. Реестра Windows в среде нет, поэтому подменяется ровно один источник —
# сами записи реестра (Test-IsWindowsHost, Test-Path, Get-ChildItem, Get-ItemProperty). Логика
# сопоставления DisplayName, экранирование подстановочных символов, выбор ветки и состав
# аргументов проверяются как есть: в Uninstall.Tests.ps1 функция Get-DesktopUninstallEntry
# замокана целиком, здесь проверяется она сама.
#
# Модель угрозы (N5-P4): app_name приходит из манифеста. Значение "*" при неэкранированном -like
# совпало бы с ПЕРВОЙ попавшейся записью Uninstall, и её UninstallString был бы запущен с
# добавленными из того же манифеста silent_args — то есть чужой деинсталлятор с чужими ключами.

BeforeAll {
    $script:InstallersRoot = (Resolve-Path (Join-Path $PSScriptRoot '..' | Join-Path -ChildPath '..')).Path
    $script:InstallScript = Join-Path (Join-Path $script:InstallersRoot 'windows') 'install.ps1'

    # Dot-source: исполняемый вход не срабатывает (N5-T2).
    . $script:InstallScript

    # Подменённый источник записей реестра: корень Uninstall -> массив записей.
    #
    # Область видимости — $script:, то есть область файла набора. Она общая для BeforeAll,
    # BeforeEach и It (на этом же держатся $script:PkgRoot/$script:HomeRoot в Install.Tests.ps1),
    # поэтому присваивание внутри It мок видит. Глобальная область здесь не нужна и запрещена
    # правилом PSAvoidGlobalVars.
    #
    # Прежняя редакция считала причиной ложных результатов именно область видимости. Причина была
    # другая (см. приведение ключа в моках ниже), и переход на $global: её не устранил.
    $script:OmFakeRoots = @{}

    # SupportsShouldProcess — требование PSScriptAnalyzer (PSUseShouldProcessForStateChangingFunctions)
    # для функций с глаголом New-. Состояния системы функция не меняет: объект строится в памяти.
    # Вызов ShouldProcess всё равно обязателен — иначе срабатывает парное правило PSShouldProcess.
    function New-RegEntry {
        [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Low')]
        param(
            [string]$Key,
            [string]$DisplayName,
            [string]$UninstallString = '',
            [string]$QuietUninstallString = '',
            [string]$InstallLocation = ''
        )
        if (-not $PSCmdlet.ShouldProcess($Key, 'Построить запись Uninstall')) { return $null }
        return [pscustomobject]@{
            PSPath               = $Key
            DisplayName          = $DisplayName
            UninstallString      = $UninstallString
            QuietUninstallString = $QuietUninstallString
            InstallLocation      = $InstallLocation
        }
    }

    # Типовое содержимое ветки Uninstall: чужие программы плюс, при желании, наша запись.
    function New-FakeRegistry {
        [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Low')]
        param([switch]$WithOpenCode)
        if (-not $PSCmdlet.ShouldProcess('HKCU/HKLM Uninstall', 'Построить содержимое ветки')) { return @{} }
        $hkcu = @(
            (New-RegEntry -Key 'HKCU:\U\7-Zip' -DisplayName '7-Zip 23.01' `
                -UninstallString 'C:\Program Files\7-Zip\Uninstall.exe' -InstallLocation 'C:\Program Files\7-Zip'),
            (New-RegEntry -Key 'HKCU:\U\Notepad++' -DisplayName 'Notepad++ (64-bit x64)' `
                -UninstallString '"C:\Program Files\Notepad++\uninstall.exe"' -InstallLocation 'C:\Program Files\Notepad++')
        )
        $hklm = @(
            (New-RegEntry -Key 'HKLM:\U\Office' -DisplayName 'Microsoft 365 Apps for enterprise' `
                -UninstallString 'MsiExec.exe /X{00000000-1111-2222-3333-444444444444}')
        )
        if ($WithOpenCode) {
            $hklm += (New-RegEntry -Key 'HKLM:\U\OpenCode' -DisplayName 'OpenCode Magnit' `
                    -UninstallString '"C:\Apps\OpenCode\Uninstall.exe"' -InstallLocation 'C:\Apps\OpenCode')
        }
        return @{
            'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' = $hkcu
            'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall' = $hklm
        }
    }

    # Манифест-объект с одним desktop-артефактом (Get-DesktopUninstallEntry больше ничего не читает).
    function New-ManifestObject {
        [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Low')]
        param([string]$AppName, [switch]$NoDesktop)
        if (-not $PSCmdlet.ShouldProcess($AppName, 'Построить объект манифеста')) { return $null }
        if ($NoDesktop) {
            return [pscustomobject]@{ artifacts = @([pscustomobject]@{ kind = 'cli' }) }
        }
        return [pscustomobject]@{
            artifacts = @(
                [pscustomobject]@{ kind = 'cli' },
                [pscustomobject]@{ kind = 'desktop'; installer_type = 'nsis'; app_name = $AppName }
            )
        }
    }

    # Временный каталог фикстуры. SupportsShouldProcess — требование PSScriptAnalyzer
    # (PSUseShouldProcessForStateChangingFunctions) для функций с глаголом New-.
    function New-TempDir {
        [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Low')]
        param()
        $path = Join-Path ([IO.Path]::GetTempPath()) ('opencode-pester-' + [Guid]::NewGuid().ToString('N'))
        if ($PSCmdlet.ShouldProcess($path, 'Создать временный каталог фикстуры')) {
            $null = New-Item -ItemType Directory -Path $path -Force
        }
        return $path
    }

    # Пакет с desktop-артефактом заданного типа и именем приложения (для Read-Manifest).
    function New-ManifestPackage {
        [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Low')]
        param(
            [string]$Root,
            [string]$InstallerType = 'dmg',
            [string]$AppName = 'OpenCode Magnit.app',
            [switch]$NoAppName
        )
        if (-not $PSCmdlet.ShouldProcess($Root, 'Собрать фикстурный пакет')) { return $Root }
        foreach ($sub in @('common', 'bin', 'certs', 'desktop')) {
            $null = New-Item -ItemType Directory -Path (Join-Path $Root $sub) -Force
        }
        $binPath = Join-Path (Join-Path $Root 'bin') 'opencode.exe'
        $caPath = Join-Path (Join-Path $Root 'certs') 'tander-ca-bundle.pem'
        $desktopPath = Join-Path (Join-Path $Root 'desktop') 'OpenCode.dmg'
        Set-Content -LiteralPath $binPath -Value 'fixture-cli' -NoNewline -Encoding ASCII
        Set-Content -LiteralPath $caPath -Value 'fixture-ca' -NoNewline -Encoding ASCII
        Set-Content -LiteralPath $desktopPath -Value 'fixture-desktop' -NoNewline -Encoding ASCII

        $desktop = [ordered]@{
            kind           = 'desktop'
            file           = 'desktop/OpenCode.dmg'
            sha256         = (Get-FileHash -LiteralPath $desktopPath -Algorithm SHA256).Hash.ToLowerInvariant()
            size           = 15
            installer_type = $InstallerType
        }
        if (-not $NoAppName) {
            $desktop['app_name'] = $AppName
        }

        $manifest = [ordered]@{
            schema         = 1
            product        = 'opencode-magnit'
            version        = '1.17.9-magnit.1'
            os             = 'windows'
            arch           = 'x64'
            hub_url        = 'https://hub.test'
            built_at       = '2026-08-18T09:41:07Z'
            source_release = 'v1.17.9-magnit.1'
            ca             = [ordered]@{
                file         = 'certs/tander-ca-bundle.pem'
                sha256       = (Get-FileHash -LiteralPath $caPath -Algorithm SHA256).Hash.ToLowerInvariant()
                install_name = 'tander-ca-bundle.pem'
            }
            artifacts      = @(
                [ordered]@{
                    kind         = 'cli'
                    file         = 'bin/opencode.exe'
                    sha256       = (Get-FileHash -LiteralPath $binPath -Algorithm SHA256).Hash.ToLowerInvariant()
                    size         = 11
                    install_name = 'opencode.exe'
                },
                $desktop
            )
            purge_paths    = @('%USERPROFILE%\.config\opencode')
        }
        Set-Content -LiteralPath (Join-Path (Join-Path $Root 'common') 'manifest.json') `
            -Value ($manifest | ConvertTo-Json -Depth 6) -Encoding UTF8
        return $Root
    }

    # Код отказа действия: 0 — успех. Вывод самого действия ПОДАВЛЯЕТСЯ ($null = ...): иначе при
    # успехе (например, Read-Manifest вернул объект манифеста) функция отдала бы в конвейер два
    # значения — объект и 0, и утверждение `| Should -Be 0` не сработало бы.
    function Get-FailureCode {
        param([scriptblock]$Action)
        try {
            $null = & $Action
        } catch {
            $data = $_.Exception.Data
            if ($null -ne $data -and $data.Contains('Code')) {
                return [int]$data['Code']
            }
            return -1
        }
        return 0
    }
}

Describe 'Сопоставление записи Uninstall с app_name (N5-R1, N5-P4)' -Tag 'ci' {
    BeforeEach {
        $script:OmFakeRoots = New-FakeRegistry
        Mock Test-IsWindowsHost { return $true }
        Mock Test-Path { return $true }
        # Ключ хеш-таблицы приводится к ОДНОЙ строке. Причина: у Get-ChildItem, Get-ItemProperty и
        # Test-Path параметр -LiteralPath объявлен как [string[]], а Pester строит мок по
        # метаданным настоящей команды — значит в теле мока $LiteralPath приходит МАССИВОМ.
        # $hash.ContainsKey(<массив>) не совпадает ни с одним строковым ключом и промахивается
        # МОЛЧА: подменённый реестр выглядит пустым, Get-DesktopUninstallEntry всегда возвращает
        # $null, тесты «совпадение найдено» краснеют, а «совпадения нет» проходят ложно-зелёными.
        Mock Get-ChildItem {
            $key = [string](@($LiteralPath)[0])
            if ($script:OmFakeRoots.ContainsKey($key)) {
                return $script:OmFakeRoots[$key]
            }
            return @()
        }
        Mock Get-ItemProperty {
            $key = [string](@($LiteralPath)[0])
            foreach ($root in @($script:OmFakeRoots.Keys)) {
                foreach ($item in @($script:OmFakeRoots[$root])) {
                    if ($item.PSPath -eq $key) { return $item }
                }
            }
            return $null
        }
    }

    AfterEach {
        $script:OmFakeRoots = @{}
    }

    It 'самоконтроль фикстуры: подменённый источник записей доходит до вызывающего кода' {
        # Проверяется сама подмена, а не продукт: без этого теста промах по ключу хеш-таблицы
        # снова выглядел бы как «записи в реестре нет» — то есть как штатный результат.
        $script:OmFakeRoots = New-FakeRegistry -WithOpenCode
        $root = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall'
        # Все три подмены, от которых зависят тесты ниже, проверяются по отдельности: иначе
        # отказ любой из них выглядит одинаково — «записи в реестре нет».
        Test-IsWindowsHost | Should -BeTrue -Because 'подмена проверки платформы действует'
        Test-Path -LiteralPath $root | Should -BeTrue -Because 'подмена наличия ветки действует'
        @(Get-ChildItem -LiteralPath $root).Count | Should -Be 2 -Because 'мок отдаёт обе записи ветки HKLM'
        (Get-ItemProperty -LiteralPath 'HKLM:\U\OpenCode').DisplayName | Should -Be 'OpenCode Magnit'
    }

    It 'AC-139: app_name="*" не совпадает ни с одной чужой записью Uninstall' {
        Get-DesktopUninstallEntry -Manifest (New-ManifestObject -AppName '*') | Should -BeNullOrEmpty
    }

    It 'AC-139: app_name="?" не совпадает ни с одной чужой записью Uninstall' {
        Get-DesktopUninstallEntry -Manifest (New-ManifestObject -AppName '?') | Should -BeNullOrEmpty
    }

    It 'AC-139: app_name="[A-z]*" не совпадает ни с одной чужой записью Uninstall' {
        Get-DesktopUninstallEntry -Manifest (New-ManifestObject -AppName '[A-z]*') | Should -BeNullOrEmpty
    }

    It 'AC-139: подстановочные символы не совпадают и когда наша запись в реестре есть' {
        $script:OmFakeRoots = New-FakeRegistry -WithOpenCode
        # Контроль ДО перебора: подменённый источник записей действительно доходит до функции.
        # Без него весь перебор был бы ложно-зелёным на пустом реестре.
        (Get-DesktopUninstallEntry -Manifest (New-ManifestObject -AppName 'OpenCode')) |
            Should -Not -BeNullOrEmpty -Because 'подменённый реестр виден функции'
        foreach ($needle in @('*', '?', '[A-z]*', '*.app', 'Open*')) {
            $entry = Get-DesktopUninstallEntry -Manifest (New-ManifestObject -AppName $needle)
            $entry | Should -BeNullOrEmpty -Because "app_name='$needle' — шаблон, а не имя"
        }
    }

    It 'AC-139: app_name="OpenCode" совпадает со своей записью (негативный контроль)' {
        $script:OmFakeRoots = New-FakeRegistry -WithOpenCode
        $entry = Get-DesktopUninstallEntry -Manifest (New-ManifestObject -AppName 'OpenCode')
        $entry | Should -Not -BeNullOrEmpty
        $entry.DisplayName | Should -Be 'OpenCode Magnit'
        $entry.UninstallString | Should -Be '"C:\Apps\OpenCode\Uninstall.exe"'
        $entry.DisplayTarget | Should -Be 'C:\Apps\OpenCode'
    }

    It 'AC-139, AC-154: суффикс .app отбрасывается — app_name="OpenCode Magnit.app" находит запись "OpenCode Magnit"' {
        $script:OmFakeRoots = New-FakeRegistry -WithOpenCode
        $entry = Get-DesktopUninstallEntry -Manifest (New-ManifestObject -AppName 'OpenCode Magnit.app')
        $entry | Should -Not -BeNullOrEmpty -Because 'пробел в штатном имени сравнение не ломает'
        $entry.DisplayName | Should -Be 'OpenCode Magnit'
    }

    It 'AC-139: нашей записи в реестре нет → $null, чужие записи не подставляются' {
        Get-DesktopUninstallEntry -Manifest (New-ManifestObject -AppName 'OpenCode') | Should -BeNullOrEmpty
    }

    It 'AC-139: без desktop-артефакта и вне Windows возвращается $null' {
        Get-DesktopUninstallEntry -Manifest (New-ManifestObject -NoDesktop) | Should -BeNullOrEmpty
        Mock Test-IsWindowsHost { return $false }
        $script:OmFakeRoots = New-FakeRegistry -WithOpenCode
        Get-DesktopUninstallEntry -Manifest (New-ManifestObject -AppName 'OpenCode') | Should -BeNullOrEmpty
    }

    It 'AC-139: пустой InstallLocation → DisplayTarget равен DisplayName' {
        $script:OmFakeRoots = @{
            'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' = @(
                (New-RegEntry -Key 'HKCU:\U\OC' -DisplayName 'OpenCode Magnit' `
                        -UninstallString '"C:\Apps\OpenCode\Uninstall.exe"')
            )
            'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall' = @()
        }
        $entry = Get-DesktopUninstallEntry -Manifest (New-ManifestObject -AppName 'OpenCode')
        $entry.DisplayTarget | Should -Be 'OpenCode Magnit'
    }
}

Describe 'Разбор UninstallString: некавыченный путь и кавычки в аргументах (N5-R1)' -Tag 'ci' {
    It 'AC-140: некавыченный путь с пробелами отделяется по расширению исполняемого файла' {
        $parsed = ConvertFrom-UninstallString -Command 'C:\Program Files\App\unins000.exe /S'
        $parsed.Path | Should -Be 'C:\Program Files\App\unins000.exe'
        (@($parsed.Arguments) -join ' ') | Should -Be '/S'
    }

    It 'AC-140: расширение распознаётся без учёта регистра и для .com/.bat/.cmd' {
        (ConvertFrom-UninstallString -Command 'C:\Program Files\App\unins000.EXE /S').Path |
            Should -Be 'C:\Program Files\App\unins000.EXE'
        (ConvertFrom-UninstallString -Command 'C:\Program Files\App\remove.cmd /quiet').Path |
            Should -Be 'C:\Program Files\App\remove.cmd'
    }

    It 'AC-140: аргумент с пробелом в кавычках остаётся одним аргументом' {
        $parsed = ConvertFrom-UninstallString -Command '"C:\Apps\App\setup.exe" /S /D="C:\Program Files\App"'
        $parsed.Path | Should -Be 'C:\Apps\App\setup.exe'
        @($parsed.Arguments).Count | Should -Be 2
        @($parsed.Arguments)[0] | Should -Be '/S'
        @($parsed.Arguments)[1] | Should -Be '/D="C:\Program Files\App"'
    }

    It 'AC-140: некавыченный путь с пробелами и аргумент в кавычках разбираются вместе' {
        $parsed = ConvertFrom-UninstallString -Command 'C:\Program Files\App\unins000.exe /D="C:\Program Files\App"'
        $parsed.Path | Should -Be 'C:\Program Files\App\unins000.exe'
        @($parsed.Arguments).Count | Should -Be 1
        @($parsed.Arguments)[0] | Should -Be '/D="C:\Program Files\App"'
    }

    It 'AC-140: неоднозначная строка (пробелы и нет границы пути) даёт $null' {
        ConvertFrom-UninstallString -Command 'C:\Program Files\App\uninstall /S' | Should -BeNullOrEmpty
        ConvertFrom-UninstallString -Command 'C:\Program Files\App uninstall' | Should -BeNullOrEmpty
    }

    It 'AC-140: пустая и пробельная строка деинсталляции дают $null' {
        ConvertFrom-UninstallString -Command '   ' | Should -BeNullOrEmpty
        ConvertFrom-UninstallString -Command '""' | Should -BeNullOrEmpty
    }
}

Describe 'Неоднозначная строка деинсталляции не запускается (N5-R1)' -Tag 'ci' {
    BeforeEach {
        $script:PkgRoot = New-TempDir
        $script:HomeRoot = New-TempDir
        $null = New-ManifestPackage -Root $script:PkgRoot -InstallerType 'nsis' -AppName 'OpenCode'
        Mock Get-UserProfileDir { return $script:HomeRoot }
        Mock Get-LocalAppDataDir { return (Join-Path $script:HomeRoot 'AppData\Local') }
        Mock Get-UserEnvironmentVariable { return $null }
        Mock Get-UserPathEntry { return @{ Value = ''; Kind = 'ExpandString' } }
        Mock Write-UserEnvironmentVariable { }
        Mock Write-UserPathEntry { }
        Mock Publish-EnvironmentChange { return $true }
        $script:Lines = New-Object System.Collections.ArrayList
        Mock Write-Line { param($Text) [void]$script:Lines.Add($Text) }
        Mock Write-Say { param($Text) [void]$script:Lines.Add($Text) }
    }

    AfterEach {
        Remove-Item -LiteralPath $script:PkgRoot -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $script:HomeRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    It 'AC-136, AC-140: неоднозначный UninstallString → деинсталлятор не запускается, «удалён» не печатается' {
        Mock Get-DesktopUninstallEntry {
            return @{
                DisplayName          = 'OpenCode Magnit'
                DisplayTarget        = 'C:\Apps\OpenCode'
                UninstallString      = 'C:\Program Files\OpenCode\uninstall /S'
                QuietUninstallString = ''
                InstallLocation      = 'C:\Apps\OpenCode'
            }
        }
        Mock Invoke-UninstallProcess { return 0 }

        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        $layout = Get-Layout -Manifest $manifest -PackageRoot $script:PkgRoot -PrefixDir ''
        $code = Invoke-Uninstall -Manifest $manifest -Layout $layout -WithPurge $false

        Should -Invoke Invoke-UninstallProcess -Times 0 -Exactly
        $text = ($script:Lines -join "`n")
        $text | Should -Match 'разобрана неоднозначно'
        $text | Should -Match 'удалите вручную'
        $text | Should -Not -Match 'Desktop: удалён'
        $code | Should -Be 0
    }

    It 'AC-136, AC-140: некавыченный путь с пробелами запускается корректно (граница по расширению)' {
        Mock Get-DesktopUninstallEntry {
            return @{
                DisplayName          = 'OpenCode Magnit'
                DisplayTarget        = 'C:\Program Files\OpenCode'
                UninstallString      = 'C:\Program Files\OpenCode\unins000.exe /uninstall'
                QuietUninstallString = ''
                InstallLocation      = 'C:\Program Files\OpenCode'
            }
        }
        Mock Invoke-UninstallProcess { return 0 }

        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        $layout = Get-Layout -Manifest $manifest -PackageRoot $script:PkgRoot -PrefixDir ''
        $code = Invoke-Uninstall -Manifest $manifest -Layout $layout -WithPurge $false

        Should -Invoke Invoke-UninstallProcess -Times 1 -Exactly -ParameterFilter {
            $Path -eq 'C:\Program Files\OpenCode\unins000.exe'
        }
        ($script:Lines -join "`n") | Should -Match 'Desktop: удалён'
        $code | Should -Be 0
    }
}

Describe 'Манифест: app_name для Desktop (N5-P4)' -Tag 'ci' {
    BeforeEach {
        $script:PkgRoot = New-TempDir
    }

    AfterEach {
        Remove-Item -LiteralPath $script:PkgRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    It 'AC-138, AC-139: installer_type="dmg" без поля app_name → код 2' {
        $null = New-ManifestPackage -Root $script:PkgRoot -InstallerType 'dmg' -NoAppName
        (Get-FailureCode { Read-Manifest -PackageRoot $script:PkgRoot }) | Should -Be 2
    }

    It 'AC-138, AC-139: installer_type="dmg" с пустым и пробельным app_name → код 2' {
        foreach ($value in @('', '   ')) {
            $root = New-TempDir
            try {
                $null = New-ManifestPackage -Root $root -InstallerType 'dmg' -AppName $value
                (Get-FailureCode { Read-Manifest -PackageRoot $root }) | Should -Be 2 -Because "app_name='$value'"
            } finally {
                Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }

    It 'AC-139: app_name с подстановочными символами → код 2' {
        foreach ($value in @('*', '?', '[A-z]*', 'Open*Code', 'OpenCode?')) {
            $root = New-TempDir
            try {
                $null = New-ManifestPackage -Root $root -InstallerType 'nsis' -AppName $value
                (Get-FailureCode { Read-Manifest -PackageRoot $root }) | Should -Be 2 -Because "app_name='$value'"
            } finally {
                Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }

    It 'AC-135, AC-139: «схлопывающиеся» и обходные значения app_name → код 2' {
        foreach ($value in @('.', './OpenCode', 'OpenCode/', 'sub/OpenCode', '../victim', 'C:\victim', '/tmp/victim')) {
            $root = New-TempDir
            try {
                $null = New-ManifestPackage -Root $root -InstallerType 'nsis' -AppName $value
                (Get-FailureCode { Read-Manifest -PackageRoot $root }) | Should -Be 2 -Because "app_name='$value'"
            } finally {
                Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }

    It 'AC-139: штатное app_name принимается для dmg и для nsis (контроль)' {
        $null = New-ManifestPackage -Root $script:PkgRoot -InstallerType 'dmg' -AppName 'OpenCode Magnit.app'
        (Get-FailureCode { Read-Manifest -PackageRoot $script:PkgRoot }) | Should -Be 0
        $root = New-TempDir
        try {
            $null = New-ManifestPackage -Root $root -InstallerType 'nsis' -AppName 'OpenCode'
            (Get-FailureCode { Read-Manifest -PackageRoot $root }) | Should -Be 0
        } finally {
            Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'AC-139: installer_type≠dmg без app_name манифест не ломает' {
        $null = New-ManifestPackage -Root $script:PkgRoot -InstallerType 'nsis' -NoAppName
        (Get-FailureCode { Read-Manifest -PackageRoot $script:PkgRoot }) | Should -Be 0
    }

    It 'AC-139: Test-AppName — таблица допустимых и запрещённых значений' {
        # Пробел внутри имени допустим (N5-P3/N5-P4), ведущий и завершающий — нет.
        foreach ($value in @('OpenCode', 'OpenCode.app', 'OpenCodeSetup.exe', 'Open Code.app', 'OpenCode Magnit.app')) {
            (Test-AppName $value) | Should -BeTrue -Because "'$value' — допустимое имя приложения"
        }
        foreach ($value in @('', '   ', '.', '..', './x', 'x/', 'a/b', '..\x', 'C:\x', '/x', '*', '?', '[', ']', 'Open*', ' a.pem', 'a.pem ', "a`tb.pem", 'a$(x).pem', 'a;b.pem', 'a&b.pem', "a'b.pem", 'a"b.pem', 'a`b.pem')) {
            (Test-AppName $value) | Should -BeFalse -Because "'$value' — недопустимое имя приложения"
        }
    }
}
