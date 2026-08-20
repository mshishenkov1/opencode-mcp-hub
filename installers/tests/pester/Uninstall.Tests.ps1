#Requires -Version 5.1
#
# installers/tests/pester/Uninstall.Tests.ps1 — удаление на Windows: штатный деинсталлятор
# Desktop (N5-R1) и безопасность app_name в манифесте (N5-P4).
#
# Прогон: Invoke-Pester installers/tests/pester (задание `pester` в installers/ci/installers.yml),
# pwsh на ubuntu-latest. Реестр Windows недоступен, поэтому граница среды подменяется двумя
# моками: Get-DesktopUninstallEntry (что зарегистрировано в системе) и Invoke-UninstallProcess
# (фактический запуск деинсталлятора и его код возврата). Всё, что между ними, — выбор ветки,
# состав аргументов, строки отчёта и код выхода — проверяется как есть.
#
# Файл к реестру и переменным окружения машины не обращается (см. тест AC-126 ниже).

BeforeAll {
    $script:PesterDir = $PSScriptRoot
    $script:InstallersRoot = (Resolve-Path (Join-Path $PSScriptRoot '..' | Join-Path -ChildPath '..')).Path
    $script:InstallScript = Join-Path (Join-Path $script:InstallersRoot 'windows') 'install.ps1'

    # Dot-source: исполняемый вход не срабатывает (N5-T2).
    . $script:InstallScript

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

    # Пакет с desktop-артефактом: app_name и silent_args задаются параметрами.
    function New-DesktopPackage {
        param(
            [string]$Root,
            [string]$AppName = 'OpenCode',
            [string[]]$SilentArgs = @('/S'),
            [string]$Version = '1.17.9-magnit.1'
        )
        foreach ($sub in @('common', 'bin', 'certs', 'desktop')) {
            $null = New-Item -ItemType Directory -Path (Join-Path $Root $sub) -Force
        }
        $binPath = Join-Path (Join-Path $Root 'bin') 'opencode.exe'
        $caPath = Join-Path (Join-Path $Root 'certs') 'tander-ca-bundle.pem'
        $desktopPath = Join-Path (Join-Path $Root 'desktop') 'OpenCodeSetup.exe'
        Set-Content -LiteralPath $binPath -Value "fixture-cli $Version" -NoNewline -Encoding ASCII
        Set-Content -LiteralPath $caPath -Value 'fixture-ca' -NoNewline -Encoding ASCII
        Set-Content -LiteralPath $desktopPath -Value 'fixture-desktop' -NoNewline -Encoding ASCII

        $desktop = [ordered]@{
            kind           = 'desktop'
            file           = 'desktop/OpenCodeSetup.exe'
            sha256         = (Get-FileHash -LiteralPath $desktopPath -Algorithm SHA256).Hash.ToLowerInvariant()
            size           = 17
            installer_type = 'nsis'
            app_name       = $AppName
        }
        if ($null -ne $SilentArgs -and $SilentArgs.Count -gt 0) {
            $desktop['silent_args'] = $SilentArgs
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
                sha256       = (Get-FileHash -LiteralPath $caPath -Algorithm SHA256).Hash.ToLowerInvariant()
                install_name = 'tander-ca-bundle.pem'
            }
            artifacts      = @(
                [ordered]@{
                    kind         = 'cli'
                    file         = 'bin/opencode.exe'
                    sha256       = (Get-FileHash -LiteralPath $binPath -Algorithm SHA256).Hash.ToLowerInvariant()
                    size         = 42
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

Describe 'Разбор UninstallString (N5-R1)' -Tag 'ci' {
    It 'AC-136: путь в кавычках отделяется от аргументов' {
        $parsed = ConvertFrom-UninstallString -Command '"C:\Program Files\OpenCode\Uninstall.exe" /S /NORESTART'
        $parsed.Path | Should -Be 'C:\Program Files\OpenCode\Uninstall.exe'
        @($parsed.Arguments).Count | Should -Be 2
        ($parsed.Arguments -join ' ') | Should -Be '/S /NORESTART'
    }

    It 'AC-136: путь в кавычках без аргументов даёт пустой список аргументов' {
        $parsed = ConvertFrom-UninstallString -Command '"C:\Apps\OpenCode\Uninstall.exe"'
        $parsed.Path | Should -Be 'C:\Apps\OpenCode\Uninstall.exe'
        @($parsed.Arguments).Count | Should -Be 0
    }

    It 'AC-136: строка MsiExec.exe /X{GUID} разбирается без кавычек' {
        $parsed = ConvertFrom-UninstallString -Command 'MsiExec.exe /X{11111111-2222-3333-4444-555555555555}'
        $parsed.Path | Should -Be 'MsiExec.exe'
        ($parsed.Arguments -join ' ') | Should -Be '/X{11111111-2222-3333-4444-555555555555}'
    }

    It 'AC-136: команда без аргументов и без кавычек остаётся путём целиком' {
        $parsed = ConvertFrom-UninstallString -Command 'C:\Apps\OpenCode\Uninstall.exe'
        $parsed.Path | Should -Be 'C:\Apps\OpenCode\Uninstall.exe'
        @($parsed.Arguments).Count | Should -Be 0
    }

    It 'AC-136: обрамляющие пробелы не попадают в путь' {
        $parsed = ConvertFrom-UninstallString -Command '   "C:\Apps\OpenCode\Uninstall.exe"   /S   '
        $parsed.Path | Should -Be 'C:\Apps\OpenCode\Uninstall.exe'
        ($parsed.Arguments -join ' ') | Should -Be '/S'
    }
}

Describe 'Штатный деинсталлятор Desktop (N5-R1)' -Tag 'ci' {
    BeforeEach {
        $script:PkgRoot = New-TempDir
        $script:HomeRoot = New-TempDir
        $null = New-DesktopPackage -Root $script:PkgRoot
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

    It 'AC-136: QuietUninstallString запускается как есть, silent_args из манифеста не добавляются' {
        Mock Get-DesktopUninstallEntry {
            return @{
                DisplayName          = 'OpenCode Magnit'
                DisplayTarget        = 'C:\Apps\OpenCode'
                UninstallString      = '"C:\Apps\OpenCode\Uninstall.exe"'
                QuietUninstallString = '"C:\Apps\OpenCode\Uninstall.exe" /VERYSILENT'
                InstallLocation      = 'C:\Apps\OpenCode'
            }
        }
        Mock Invoke-UninstallProcess { return 0 }

        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        $layout = Get-Layout -Manifest $manifest -PackageRoot $script:PkgRoot -PrefixDir ''
        $code = Invoke-Uninstall -Manifest $manifest -Layout $layout -WithPurge $false

        Should -Invoke Invoke-UninstallProcess -Times 1 -Exactly -ParameterFilter {
            $Path -eq 'C:\Apps\OpenCode\Uninstall.exe' -and (@($Arguments) -join ' ') -eq '/VERYSILENT'
        }
        ($script:Lines -join "`n") | Should -Match 'Desktop: удалён \(C:\\Apps\\OpenCode\)'
        ($script:Lines -join "`n") | Should -Not -Match 'удалите вручную'
        $code | Should -Be 0
    }

    It 'AC-136: без QuietUninstallString берётся UninstallString и к нему добавляются silent_args' {
        Mock Get-DesktopUninstallEntry {
            return @{
                DisplayName          = 'OpenCode Magnit'
                DisplayTarget        = 'C:\Apps\OpenCode'
                UninstallString      = '"C:\Apps\OpenCode\Uninstall.exe" /uninstall'
                QuietUninstallString = ''
                InstallLocation      = 'C:\Apps\OpenCode'
            }
        }
        Mock Invoke-UninstallProcess { return 0 }

        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        $layout = Get-Layout -Manifest $manifest -PackageRoot $script:PkgRoot -PrefixDir ''
        $code = Invoke-Uninstall -Manifest $manifest -Layout $layout -WithPurge $false

        Should -Invoke Invoke-UninstallProcess -Times 1 -Exactly -ParameterFilter {
            $Path -eq 'C:\Apps\OpenCode\Uninstall.exe' -and (@($Arguments) -join ' ') -eq '/uninstall /S'
        }
        ($script:Lines -join "`n") | Should -Match 'Desktop: удалён'
        $code | Should -Be 0
    }

    It 'AC-136: записи в реестре нет → процесс не запускается, печатается «удалите вручную», код 0' {
        Mock Get-DesktopUninstallEntry { return $null }
        Mock Invoke-UninstallProcess { return 0 }

        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        $layout = Get-Layout -Manifest $manifest -PackageRoot $script:PkgRoot -PrefixDir ''
        $code = Invoke-Uninstall -Manifest $manifest -Layout $layout -WithPurge $false

        Should -Invoke Invoke-UninstallProcess -Times 0 -Exactly
        $text = $script:Lines -join "`n"
        $text | Should -Match 'Desktop: удалите вручную через «Приложения и возможности»'
        $text | Should -Not -Match 'Desktop: удалён'
        $text | Should -Match 'Готово: OpenCode удалён\.'
        $code | Should -Be 0
    }

    It 'AC-136: пустой UninstallString при пустом QuietUninstallString → «удалите вручную», код 0' {
        Mock Get-DesktopUninstallEntry {
            return @{
                DisplayName          = 'OpenCode Magnit'
                DisplayTarget        = 'OpenCode Magnit'
                UninstallString      = ''
                QuietUninstallString = ''
                InstallLocation      = ''
            }
        }
        Mock Invoke-UninstallProcess { return 0 }

        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        $layout = Get-Layout -Manifest $manifest -PackageRoot $script:PkgRoot -PrefixDir ''
        $code = Invoke-Uninstall -Manifest $manifest -Layout $layout -WithPurge $false

        Should -Invoke Invoke-UninstallProcess -Times 0 -Exactly
        ($script:Lines -join "`n") | Should -Match 'Desktop: удалите вручную'
        $code | Should -Be 0
    }

    It 'AC-137: ненулевой код деинсталлятора → «Desktop: ошибка (код 3)» и код выхода 1' {
        Mock Get-DesktopUninstallEntry {
            return @{
                DisplayName          = 'OpenCode Magnit'
                DisplayTarget        = 'C:\Apps\OpenCode'
                UninstallString      = '"C:\Apps\OpenCode\Uninstall.exe"'
                QuietUninstallString = '"C:\Apps\OpenCode\Uninstall.exe" /VERYSILENT'
                InstallLocation      = 'C:\Apps\OpenCode'
            }
        }
        Mock Invoke-UninstallProcess { return 3 }

        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        $layout = Get-Layout -Manifest $manifest -PackageRoot $script:PkgRoot -PrefixDir ''
        # CLI, .bak и CA существуют: их удаление не откатывается из-за провала деинсталлятора.
        $null = New-Item -ItemType Directory -Path $layout.BinDir -Force
        $null = New-Item -ItemType Directory -Path $layout.ConfigDir -Force
        Set-Content -LiteralPath $layout.BinTarget -Value 'cli' -NoNewline -Encoding ASCII
        Set-Content -LiteralPath $layout.CaTarget -Value 'ca' -NoNewline -Encoding ASCII
        # Значения для моков кладутся в script-область: тело мока выполняется вне области It.
        $script:PathValue = $layout.BinDir
        $script:EnvValue = $layout.CaTarget
        Mock Get-UserPathEntry { return @{ Value = $script:PathValue; Kind = 'ExpandString' } }
        Mock Get-UserEnvironmentVariable { return $script:EnvValue }

        $code = Invoke-Uninstall -Manifest $manifest -Layout $layout -WithPurge $false

        $text = $script:Lines -join "`n"
        $text | Should -Match 'Desktop: ошибка \(код 3\)'
        $text | Should -Not -Match 'Desktop: удалён'
        $text | Should -Match 'Бинарник: удалён'
        $text | Should -Match 'Каталог в PATH: удалён'
        $text | Should -Match 'NODE_EXTRA_CA_CERTS: удалён'
        $text | Should -Match 'CA: удалён'
        $text | Should -Match 'Готово: OpenCode удалён\.'
        (Test-Path -LiteralPath $layout.BinTarget) | Should -BeFalse
        (Test-Path -LiteralPath $layout.CaTarget) | Should -BeFalse
        $code | Should -Be 1
    }

    It 'AC-137: успешный деинсталлятор при том же наборе шагов даёт код 0' {
        Mock Get-DesktopUninstallEntry {
            return @{
                DisplayName          = 'OpenCode Magnit'
                DisplayTarget        = 'C:\Apps\OpenCode'
                UninstallString      = '"C:\Apps\OpenCode\Uninstall.exe"'
                QuietUninstallString = '"C:\Apps\OpenCode\Uninstall.exe" /VERYSILENT'
                InstallLocation      = 'C:\Apps\OpenCode'
            }
        }
        Mock Invoke-UninstallProcess { return 0 }

        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        $layout = Get-Layout -Manifest $manifest -PackageRoot $script:PkgRoot -PrefixDir ''
        $code = Invoke-Uninstall -Manifest $manifest -Layout $layout -WithPurge $false
        ($script:Lines -join "`n") | Should -Not -Match 'Desktop: ошибка'
        $code | Should -Be 0
    }

    It 'AC-136: без desktop-артефакта деинсталлятор не ищется и не запускается' {
        $plainRoot = New-TempDir
        try {
            $null = New-DesktopPackage -Root $plainRoot
            # Убираем desktop-артефакт из манифеста, оставляя один cli.
            $file = Join-Path (Join-Path $plainRoot 'common') 'manifest.json'
            $manifestObject = (Get-Content -LiteralPath $file -Raw) | ConvertFrom-Json
            $manifestObject.artifacts = @($manifestObject.artifacts[0])
            Set-Content -LiteralPath $file -Value ($manifestObject | ConvertTo-Json -Depth 6) -Encoding UTF8

            Mock Invoke-UninstallProcess { return 0 }
            $manifest = Read-Manifest -PackageRoot $plainRoot
            $layout = Get-Layout -Manifest $manifest -PackageRoot $plainRoot -PrefixDir ''
            $code = Invoke-Uninstall -Manifest $manifest -Layout $layout -WithPurge $false

            Should -Invoke Invoke-UninstallProcess -Times 0 -Exactly
            ($script:Lines -join "`n") | Should -Match 'Desktop: не входит в пакет'
            $code | Should -Be 0
        } finally {
            Remove-Item -LiteralPath $plainRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Describe 'Безопасность app_name в манифесте (N5-P4)' -Tag 'ci' {
    BeforeEach {
        $script:PkgRoot = New-TempDir
    }

    AfterEach {
        Remove-Item -LiteralPath $script:PkgRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    It 'AC-135: app_name с обходом каталога отвергается Read-Manifest кодом 2' {
        $null = New-DesktopPackage -Root $script:PkgRoot -AppName '../../victim'
        (Get-FailureCode { Read-Manifest -PackageRoot $script:PkgRoot }) | Should -Be 2
    }

    It 'AC-135: app_name с ведущим слэшем отвергается кодом 2' {
        $null = New-DesktopPackage -Root $script:PkgRoot -AppName '/tmp/victim'
        (Get-FailureCode { Read-Manifest -PackageRoot $script:PkgRoot }) | Should -Be 2
    }

    It 'AC-135: app_name с обратным слэшем и с буквой диска отвергается кодом 2' {
        $null = New-DesktopPackage -Root $script:PkgRoot -AppName '..\victim'
        (Get-FailureCode { Read-Manifest -PackageRoot $script:PkgRoot }) | Should -Be 2
        Remove-Item -LiteralPath $script:PkgRoot -Recurse -Force
        $script:PkgRoot = New-TempDir
        $null = New-DesktopPackage -Root $script:PkgRoot -AppName 'C:\victim'
        (Get-FailureCode { Read-Manifest -PackageRoot $script:PkgRoot }) | Should -Be 2
    }

    It 'AC-135: сообщение называет поле artifacts.1.app_name' {
        $null = New-DesktopPackage -Root $script:PkgRoot -AppName '../../victim'
        $message = ''
        try {
            $null = Read-Manifest -PackageRoot $script:PkgRoot
        } catch {
            $message = $_.Exception.Message
        }
        $message | Should -Match 'artifacts\.1\.app_name'
        $message | Should -Match 'недопустимый путь'
    }

    It 'AC-135: штатное app_name принимается' {
        $null = New-DesktopPackage -Root $script:PkgRoot -AppName 'OpenCode'
        $manifest = Read-Manifest -PackageRoot $script:PkgRoot
        (Get-Prop (Get-Artifact $manifest 'desktop') 'app_name') | Should -Be 'OpenCode'
    }

    It 'AC-126: набор не обращается к реестру и не пишет переменные окружения' {
        # Обращения к реестру в наборе есть только через Mock (подменённый источник записей
        # в DesktopEntry.Tests.ps1); настоящих ЗАПИСЕЙ быть не должно ни в одном файле.
        # Строки самих утверждений исключаются — иначе тест сработал бы на своём же тексте.
        $files = @(Get-ChildItem -LiteralPath $script:PesterDir -Filter '*.Tests.ps1')
        $files.Count | Should -BeGreaterThan 0
        foreach ($file in $files) {
            $lines = @(Get-Content -LiteralPath $file.FullName |
                    Where-Object { $_ -notmatch 'Should -Not -Match' })
            $text = ($lines -join "`n")
            $text | Should -Not -Match '\[Environment\]::SetEnvironmentVariable' -Because "$($file.Name)"
            $text | Should -Not -Match '(Set|New|Remove)-ItemProperty' -Because "$($file.Name)"
            $text | Should -Not -Match 'New-PSDrive' -Because "$($file.Name)"
        }
    }
}
