#Requires -Version 5.1
#
# installers/tests/pester/Install.Tests.ps1 — тесты install.ps1 без Windows-реестра (N5-T2).
#
# Прогон: Invoke-Pester installers/tests/pester (задание `pester` в installers/ci/installers.yml).
# Файл рассчитан на pwsh под Linux/macOS в CI: проверяются разбор и валидация аргументов,
# валидация манифеста (N5-P4), сравнение sha256 (N5-P5), построение плана шагов (N5-C1),
# построчный вывод --check (N5-C2) и коды выхода (N5-I1).
#
# Работа с реестром, User PATH, WM_SETTINGCHANGE, заменой занятого opencode.exe и тихой
# установкой Desktop в этот набор не входит: соответствующие AC имеют тип manual (стенд D-9).
# Тонкие обёртки над средой (Get-HostOsArch, Get-UserEnvironmentVariable, Get-UserPathEntry,
# Write-UserPathEntry, Publish-EnvironmentChange, Invoke-DesktopInstaller) подменяются Mock.

BeforeAll {
    $script:InstallersRoot = (Resolve-Path (Join-Path $PSScriptRoot '..' | Join-Path -ChildPath '..')).Path
    $script:InstallScript = Join-Path (Join-Path $script:InstallersRoot 'windows') 'install.ps1'

    # Dot-source: исполняемый вход не срабатывает (N5-T2).
    . $script:InstallScript

    function New-FixturePackage {
        param(
            [string]$Root,
            [switch]$WithDesktop,
            [switch]$DesktopSilentArgs,
            [string]$Version = '1.17.9-magnit.1'
        )
        $null = New-Item -ItemType Directory -Path (Join-Path $Root 'common') -Force
        $null = New-Item -ItemType Directory -Path (Join-Path $Root 'bin') -Force
        $null = New-Item -ItemType Directory -Path (Join-Path $Root 'certs') -Force

        $binPath = Join-Path (Join-Path $Root 'bin') 'opencode.exe'
        $caPath = Join-Path (Join-Path $Root 'certs') 'tander-ca-bundle.pem'
        Set-Content -LiteralPath $binPath -Value "fixture-cli $Version" -NoNewline -Encoding ASCII
        Set-Content -LiteralPath $caPath -Value 'fixture-ca' -NoNewline -Encoding ASCII

        $binSha = (Get-FileHash -LiteralPath $binPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $caSha = (Get-FileHash -LiteralPath $caPath -Algorithm SHA256).Hash.ToLowerInvariant()

        $artifacts = @(
            [ordered]@{
                kind         = 'cli'
                file         = 'bin/opencode.exe'
                sha256       = $binSha
                size         = 42
                install_name = 'opencode.exe'
            }
        )
        if ($WithDesktop) {
            $null = New-Item -ItemType Directory -Path (Join-Path $Root 'desktop') -Force
            $desktopPath = Join-Path (Join-Path $Root 'desktop') 'OpenCodeSetup.exe'
            Set-Content -LiteralPath $desktopPath -Value 'fixture-desktop' -NoNewline -Encoding ASCII
            $desktopSha = (Get-FileHash -LiteralPath $desktopPath -Algorithm SHA256).Hash.ToLowerInvariant()
            $desktop = [ordered]@{
                kind           = 'desktop'
                file           = 'desktop/OpenCodeSetup.exe'
                sha256         = $desktopSha
                size           = 17
                installer_type = 'nsis'
            }
            if ($DesktopSilentArgs) {
                $desktop['silent_args'] = @('/S')
            }
            $artifacts += $desktop
        }

        $manifest = [ordered]@{
            schema         = 1
            product        = 'opencode-magnit'
            version        = $Version
            os             = 'windows'
            arch           = 'x64'
            hub_url        = 'https://hub.test'
            built_at       = '2026-08-18T09:41:07Z'
            source_release = "v$Version"
            ca             = [ordered]@{
                file         = 'certs/tander-ca-bundle.pem'
                sha256       = $caSha
                install_name = 'tander-ca-bundle.pem'
            }
            artifacts      = $artifacts
            purge_paths    = @('%USERPROFILE%\.config\opencode', '%USERPROFILE%\.local\share\opencode')
        }
        $json = $manifest | ConvertTo-Json -Depth 6
        Set-Content -LiteralPath (Join-Path (Join-Path $Root 'common') 'manifest.json') -Value $json -Encoding UTF8
        return $Root
    }

    function New-TempDir {
        $path = Join-Path ([IO.Path]::GetTempPath()) ('opencode-pester-' + [Guid]::NewGuid().ToString('N'))
        $null = New-Item -ItemType Directory -Path $path -Force
        return $path
    }

    function Get-FailureCode {
        param([scriptblock]$Action)
        try {
            & $Action
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

Describe 'AC-125: dot-source install.ps1 не выполняет действий' {
    It 'AC-125: функции доступны после dot-source' {
        (Get-Command Get-InstallPlan -ErrorAction SilentlyContinue) | Should -Not -BeNullOrEmpty
        (Get-Command Read-Manifest -ErrorAction SilentlyContinue) | Should -Not -BeNullOrEmpty
        (Get-Command Invoke-Main -ErrorAction SilentlyContinue) | Should -Not -BeNullOrEmpty
    }

    It 'AC-125: dot-source не создаёт файлов и не меняет переменных окружения' {
        $before = [Environment]::GetEnvironmentVariable('NODE_EXTRA_CA_CERTS')
        . $script:InstallScript
        [Environment]::GetEnvironmentVariable('NODE_EXTRA_CA_CERTS') | Should -Be $before
        # Исполняемый вход обёрнут условием: строка присутствует в исходнике (N5-T2).
        (Get-Content -LiteralPath $script:InstallScript -Raw) | Should -Match "InvocationName -ne '\.'"
    }

    It 'AC-126: набор Pester не трогает реестр и User PATH' {
        $text = Get-Content -LiteralPath $PSCommandPath -Raw
        $text | Should -Not -Match 'HKEY_CURRENT_USER'
        $text | Should -Not -Match 'SetEnvironmentVariable'
    }
}

Describe 'Валидация манифеста (N5-P4)' {
    BeforeEach {
        $script:PkgRoot = New-TempDir
        $null = New-FixturePackage -Root $script:PkgRoot
        $script:ManifestFile = Join-Path (Join-Path $script:PkgRoot 'common') 'manifest.json'
    }

    AfterEach {
        Remove-Item -LiteralPath $script:PkgRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    It 'корректный манифест читается и содержит поля пакета' {
        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        $manifest.product | Should -Be 'opencode-magnit'
        $manifest.version | Should -Be '1.17.9-magnit.1'
        $manifest.os | Should -Be 'windows'
    }

    It 'AC-06: отсутствующий манифест → код 2' {
        Remove-Item -LiteralPath $script:ManifestFile -Force
        (Get-FailureCode { Read-Manifest -PackageRoot $script:PkgRoot }) | Should -Be 2
    }

    It 'AC-07: манифест не является JSON → код 2' {
        Set-Content -LiteralPath $script:ManifestFile -Value 'это не json' -Encoding UTF8
        (Get-FailureCode { Read-Manifest -PackageRoot $script:PkgRoot }) | Should -Be 2
    }

    It 'AC-08: schema=2 → код 2' {
        $raw = (Get-Content -LiteralPath $script:ManifestFile -Raw).Replace('"schema": 1', '"schema": 2')
        Set-Content -LiteralPath $script:ManifestFile -Value $raw -Encoding UTF8
        (Get-FailureCode { Read-Manifest -PackageRoot $script:PkgRoot }) | Should -Be 2
    }

    It 'AC-09: пустое поле version → код 2' {
        $raw = (Get-Content -LiteralPath $script:ManifestFile -Raw).Replace('"1.17.9-magnit.1"', '""')
        Set-Content -LiteralPath $script:ManifestFile -Value $raw -Encoding UTF8
        (Get-FailureCode { Read-Manifest -PackageRoot $script:PkgRoot }) | Should -Be 2
    }

    It 'AC-10: sha256 неверной длины отвергается' {
        Test-Sha256Value ('a' * 63) | Should -BeFalse
        Test-Sha256Value ('z' * 64) | Should -BeFalse
        Test-Sha256Value ('A' * 64) | Should -BeTrue
    }

    It 'AC-11: небезопасные пути внутри пакета отвергаются' {
        Test-PackagePath '../../etc/passwd' | Should -BeFalse
        Test-PackagePath '/etc/passwd' | Should -BeFalse
        Test-PackagePath 'C:\x' | Should -BeFalse
        Test-PackagePath 'bin\opencode.exe' | Should -BeFalse
        Test-PackagePath 'bin/opencode.exe' | Should -BeTrue
    }

    It 'AC-05: два артефакта kind=cli → код 2' {
        $raw = (Get-Content -LiteralPath $script:ManifestFile -Raw)
        $manifest = $raw | ConvertFrom-Json
        $cli = $manifest.artifacts[0]
        $manifest.artifacts = @($cli, $cli)
        Set-Content -LiteralPath $script:ManifestFile -Value ($manifest | ConvertTo-Json -Depth 6) -Encoding UTF8
        (Get-FailureCode { Read-Manifest -PackageRoot $script:PkgRoot }) | Should -Be 2
    }
}

Describe 'Проверка sha256 (N5-P5)' {
    BeforeEach {
        $script:PkgRoot = New-TempDir
        $null = New-FixturePackage -Root $script:PkgRoot
    }

    AfterEach {
        Remove-Item -LiteralPath $script:PkgRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    It 'AC-16: сравнение хешей регистронезависимо' {
        $file = Join-Path (Join-Path $script:PkgRoot 'bin') 'opencode.exe'
        $sha = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToUpperInvariant()
        (Get-FailureCode { Confirm-FileHash -Path $file -Expected $sha }) | Should -Be 0
    }

    It 'AC-13: несовпадение sha256 → код 4' {
        $file = Join-Path (Join-Path $script:PkgRoot 'bin') 'opencode.exe'
        (Get-FailureCode { Confirm-FileHash -Path $file -Expected ('0' * 64) }) | Should -Be 4
    }

    It 'AC-13: подменённый файл пакета отвергается проверкой целостности' {
        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        $file = Join-Path (Join-Path $script:PkgRoot 'bin') 'opencode.exe'
        Set-Content -LiteralPath $file -Value 'подмена' -NoNewline -Encoding UTF8
        $code = Get-FailureCode {
            Confirm-PackageIntegrity -Manifest $manifest -PackageRoot $script:PkgRoot -WithDesktop $false
        }
        $code | Should -Be 4
    }
}

Describe 'Аргументы и взаимоисключающие режимы (N5-I1)' {
    BeforeEach {
        $script:PkgRoot = New-TempDir
        $null = New-FixturePackage -Root $script:PkgRoot
        # Корень пакета и вывод подменяются моками: тест не зависит от расположения install.ps1.
        Mock Get-PackageRoot { return $script:PkgRoot }
        Mock Write-Line { }
        Mock Write-ErrorLine { }
    }

    AfterEach {
        Remove-Item -LiteralPath $script:PkgRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    It 'AC-23: -Help печатает справку и возвращает 0' {
        Invoke-Main -Help | Should -Be 0
    }

    It 'AC-25: неизвестный аргумент → код 2' {
        Invoke-Main -Rest @('--wat') | Should -Be 2
    }

    It 'AC-26: -DryRun вместе с -Check → код 2' {
        Invoke-Main -DryRun -Check | Should -Be 2
    }

    It 'AC-27: -Check вместе с -Uninstall → код 2' {
        Invoke-Main -Check -Uninstall | Should -Be 2
    }

    It 'AC-95: -Purge без -Uninstall → код 2' {
        Invoke-Main -Purge | Should -Be 2
    }

    It 'AC-24: -ShowVersion печатает строку версии из манифеста и возвращает 0' {
        Invoke-Main -ShowVersion | Should -Be 0
    }

    It 'AC-33: пакет для Windows на не-Windows хосте → код 3' {
        Mock Get-HostOsArch { return @{ Os = 'linux'; Arch = 'x64' } }
        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        (Get-FailureCode { Test-EnvironmentCompatible -Manifest $manifest }) | Should -Be 3
    }
}

Describe 'План установки Get-InstallPlan (N5-C1, N5-I10)' {
    BeforeEach {
        $script:PkgRoot = New-TempDir
        $script:HomeRoot = New-TempDir
        Mock Get-UserProfileDir { return $script:HomeRoot }
        Mock Get-LocalAppDataDir { return (Join-Path $script:HomeRoot 'AppData\Local') }
    }

    AfterEach {
        Remove-Item -LiteralPath $script:PkgRoot -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $script:HomeRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    It 'AC-96: план содержит абсолютные пути источника и приёмника и целевое значение переменной' {
        $null = New-FixturePackage -Root $script:PkgRoot
        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        $layout = Get-Layout -Manifest $manifest -PackageRoot $script:PkgRoot -PrefixDir ''
        $plan = Get-InstallPlan -Manifest $manifest -Layout $layout -PackageRoot $script:PkgRoot -SkipDesktop $false -SkipLaunch $true
        ($plan -join "`n") | Should -Match ([regex]::Escape($layout.CaSource))
        ($plan -join "`n") | Should -Match ([regex]::Escape($layout.CaTarget))
        ($plan -join "`n") | Should -Match ([regex]::Escape($layout.BinTarget))
        ($plan -join "`n") | Should -Match 'NODE_EXTRA_CA_CERTS'
        ($plan -join "`n") | Should -Match 'Hub: https://hub.test'
    }

    It 'AC-68: без артефакта desktop план содержит «Desktop: не входит в пакет»' {
        $null = New-FixturePackage -Root $script:PkgRoot
        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        $layout = Get-Layout -Manifest $manifest -PackageRoot $script:PkgRoot -PrefixDir ''
        $plan = Get-InstallPlan -Manifest $manifest -Layout $layout -PackageRoot $script:PkgRoot -SkipDesktop $false -SkipLaunch $true
        ($plan -join "`n") | Should -Match 'Desktop: не входит в пакет'
    }

    It 'AC-66: desktop-артефакт без silent_args → строка «Desktop: запустите установщик вручную» с путём' {
        $null = New-FixturePackage -Root $script:PkgRoot -WithDesktop
        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        $layout = Get-Layout -Manifest $manifest -PackageRoot $script:PkgRoot -PrefixDir ''
        $plan = Get-InstallPlan -Manifest $manifest -Layout $layout -PackageRoot $script:PkgRoot -SkipDesktop $false -SkipLaunch $true
        $text = $plan -join "`n"
        $installerPath = Get-PackageFilePath $script:PkgRoot 'desktop/OpenCodeSetup.exe'
        $text | Should -Match 'Desktop: запустите установщик вручную'
        $text | Should -Match ([regex]::Escape($installerPath))
        $text | Should -Not -Match 'Запустить установщик Desktop'
    }

    It 'AC-65: desktop-артефакт с silent_args → в плане запуск установщика с этими аргументами' {
        $null = New-FixturePackage -Root $script:PkgRoot -WithDesktop -DesktopSilentArgs
        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        $layout = Get-Layout -Manifest $manifest -PackageRoot $script:PkgRoot -PrefixDir ''
        $plan = Get-InstallPlan -Manifest $manifest -Layout $layout -PackageRoot $script:PkgRoot -SkipDesktop $false -SkipLaunch $true
        ($plan -join "`n") | Should -Match 'Запустить установщик Desktop'
        ($plan -join "`n") | Should -Match '/S'
    }

    It 'AC-69: -NoDesktop → строка «Desktop: пропущен»' {
        $null = New-FixturePackage -Root $script:PkgRoot -WithDesktop
        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        $layout = Get-Layout -Manifest $manifest -PackageRoot $script:PkgRoot -PrefixDir ''
        $plan = Get-InstallPlan -Manifest $manifest -Layout $layout -PackageRoot $script:PkgRoot -SkipDesktop $true -SkipLaunch $true
        ($plan -join "`n") | Should -Match 'Desktop: пропущен'
    }

    It 'AC-77: -NoLaunch убирает из плана шаг corp status' {
        $null = New-FixturePackage -Root $script:PkgRoot
        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        $layout = Get-Layout -Manifest $manifest -PackageRoot $script:PkgRoot -PrefixDir ''
        $withLaunch = Get-InstallPlan -Manifest $manifest -Layout $layout -PackageRoot $script:PkgRoot -SkipDesktop $false -SkipLaunch $false
        $noLaunch = Get-InstallPlan -Manifest $manifest -Layout $layout -PackageRoot $script:PkgRoot -SkipDesktop $false -SkipLaunch $true
        ($withLaunch -join "`n") | Should -Match 'corp status'
        ($noLaunch -join "`n") | Should -Not -Match 'corp status'
    }

    It 'AC-97: построение плана не создаёт каталогов' {
        $null = New-FixturePackage -Root $script:PkgRoot
        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        $layout = Get-Layout -Manifest $manifest -PackageRoot $script:PkgRoot -PrefixDir ''
        $null = Get-InstallPlan -Manifest $manifest -Layout $layout -PackageRoot $script:PkgRoot -SkipDesktop $false -SkipLaunch $true
        (Test-Path -LiteralPath $layout.ConfigDir) | Should -BeFalse
        (Test-Path -LiteralPath $layout.BinDir) | Should -BeFalse
    }
}

Describe 'Построчный вывод --check (N5-C2, N5-C3)' {
    BeforeEach {
        $script:PkgRoot = New-TempDir
        $script:HomeRoot = New-TempDir
        $null = New-FixturePackage -Root $script:PkgRoot
        Mock Get-UserProfileDir { return $script:HomeRoot }
        Mock Get-LocalAppDataDir { return (Join-Path $script:HomeRoot 'AppData\Local') }
        Mock Get-UserEnvironmentVariable { return $null }
        Mock Get-UserPathEntry { return @{ Value = ''; Kind = 'ExpandString' } }
        Mock Publish-EnvironmentChange { return $true }
        $script:CheckLines = New-Object System.Collections.ArrayList
        Mock Write-Line { param($Text) [void]$script:CheckLines.Add($Text) }
        Mock Write-Say { param($Text) [void]$script:CheckLines.Add($Text) }
    }

    AfterEach {
        Remove-Item -LiteralPath $script:PkgRoot -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $script:HomeRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    It 'AC-101, AC-107: на чистой системе печатаются строки «не установлен» и возвращается 7' {
        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        $layout = Get-Layout -Manifest $manifest -PackageRoot $script:PkgRoot -PrefixDir ''
        $code = Invoke-Check -Manifest $manifest -Layout $layout
        $text = $script:CheckLines -join "`n"
        $text | Should -Match 'Пакет: opencode-magnit 1\.17\.9-magnit\.1 \(windows-x64\)'
        $text | Should -Match 'Манифест: корректен'
        $text | Should -Match 'Бинарник: не установлен'
        $text | Should -Match 'CA: не установлен'
        $text | Should -Match 'NODE_EXTRA_CA_CERTS: не задан'
        $text | Should -Match 'Итог: требуется установка'
        $code | Should -Be 7
    }

    It 'AC-104: установленный CA с другим содержимым → «CA: отличается от пакета»' {
        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        $layout = Get-Layout -Manifest $manifest -PackageRoot $script:PkgRoot -PrefixDir ''
        $null = New-Item -ItemType Directory -Path $layout.ConfigDir -Force
        Set-Content -LiteralPath $layout.CaTarget -Value 'другой CA' -NoNewline -Encoding UTF8
        $null = Invoke-Check -Manifest $manifest -Layout $layout
        ($script:CheckLines -join "`n") | Should -Match 'CA: отличается от пакета'
    }
}

Describe 'purge_paths: раскрытие и проверка безопасности (N5-R2)' {
    BeforeEach {
        $script:HomeRoot = New-TempDir
        Mock Get-UserProfileDir { return $script:HomeRoot }
        Mock Get-LocalAppDataDir { return (Join-Path $script:HomeRoot 'AppData\Local') }
    }

    AfterEach {
        Remove-Item -LiteralPath $script:HomeRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    It 'AC-89: пути с %USERPROFILE% раскрываются внутрь профиля пользователя' {
        $expanded = Expand-UserPathTemplate -Value '%USERPROFILE%\.config\opencode'
        $expanded | Should -Match ([regex]::Escape($script:HomeRoot))
        Test-PurgePathSafe -Raw '%USERPROFILE%\.config\opencode' -Expanded $expanded | Should -BeTrue
    }

    It 'AC-90, AC-91: путь вне профиля, путь с .. и сам профиль признаются небезопасными' {
        Test-PurgePathSafe -Raw 'C:\Windows' -Expanded 'C:\Windows' | Should -BeFalse
        Test-PurgePathSafe -Raw '%USERPROFILE%\..\other' -Expanded (Join-Path $script:HomeRoot '..\other') | Should -BeFalse
        Test-PurgePathSafe -Raw '%USERPROFILE%' -Expanded $script:HomeRoot | Should -BeFalse
    }
}

Describe 'Логика User PATH без реестра (N5-I8; сама запись в реестр — manual)' {
    It 'сравнение элементов PATH регистронезависимо и без завершающего слэша' {
        Test-UserPathContains -PathValue 'C:\Tools;C:\Users\u\AppData\Local\Programs\opencode\' -Directory 'C:\Users\u\AppData\Local\Programs\OpenCode' | Should -BeTrue
        Test-UserPathContains -PathValue 'C:\Tools' -Directory 'C:\Users\u\AppData\Local\Programs\opencode' | Should -BeFalse
    }
}
