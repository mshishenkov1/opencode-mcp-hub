#Requires -Version 5.1
#
# installers/tests/pester/Encoding.Tests.ps1 — кодировки и переводы строк (N5-T6).
#
# install.ps1 обязан быть UTF-8 с BOM (иначе PowerShell 5.1 читает файл в кодировке системы
# и русские строки отчёта превращаются в мусор), install.bat — только ASCII,
# файлы *.sh — UTF-8 без BOM с переводами строк LF.

BeforeAll {
    $script:InstallersRoot = (Resolve-Path (Join-Path $PSScriptRoot '..' | Join-Path -ChildPath '..')).Path
    $script:Ps1Path = Join-Path (Join-Path $script:InstallersRoot 'windows') 'install.ps1'
    $script:BatPath = Join-Path (Join-Path $script:InstallersRoot 'windows') 'install.bat'
}

Describe 'Кодировки файлов (N5-T6)' {
    It 'AC-133: install.ps1 начинается с BOM EF BB BF' {
        $bytes = [IO.File]::ReadAllBytes($script:Ps1Path)
        $bytes.Length | Should -BeGreaterThan 3
        $bytes[0] | Should -Be 0xEF
        $bytes[1] | Should -Be 0xBB
        $bytes[2] | Should -Be 0xBF
    }

    It 'AC-133: русские строки install.ps1 читаются как UTF-8' {
        $text = Get-Content -LiteralPath $script:Ps1Path -Raw -Encoding UTF8
        $text | Should -Match 'Готово: OpenCode'
        $text | Should -Match 'Итог: всё установлено'
        $text | Should -Not -Match 'РџСЂ'
    }

    It 'AC-32: install.bat состоит только из ASCII и запускает install.ps1 в обход политики' {
        $bytes = [IO.File]::ReadAllBytes($script:BatPath)
        ($bytes | Where-Object { $_ -ge 0x80 }).Count | Should -Be 0
        $text = Get-Content -LiteralPath $script:BatPath -Raw
        $text | Should -Match ([regex]::Escape('powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*'))
        $text | Should -Match ([regex]::Escape('exit /b %ERRORLEVEL%'))
    }

    It 'AC-134: все *.sh без BOM, с shebang и без символов CR' {
        $files = Get-ChildItem -Path $script:InstallersRoot -Filter '*.sh' -Recurse -File |
            Where-Object { $_.FullName -notmatch '[\\/]dist[\\/]' }
        $files.Count | Should -BeGreaterThan 0
        foreach ($file in $files) {
            $bytes = [IO.File]::ReadAllBytes($file.FullName)
            if ($bytes.Length -ge 3) {
                (($bytes[0] -eq 0xEF) -and ($bytes[1] -eq 0xBB) -and ($bytes[2] -eq 0xBF)) | Should -BeFalse -Because "BOM в $($file.FullName)"
            }
            $bytes[0] | Should -Be 0x23 -Because "нет shebang в $($file.FullName)"
            $bytes[1] | Should -Be 0x21 -Because "нет shebang в $($file.FullName)"
            ($bytes | Where-Object { $_ -eq 0x0D }).Count | Should -Be 0 -Because "символы CR в $($file.FullName)"
        }
    }

    It 'AC-132: install.ps1 требует PowerShell 5.1 и не содержит конструкций 6+' {
        $text = Get-Content -LiteralPath $script:Ps1Path -Raw -Encoding UTF8
        $text | Should -Match '#Requires -Version 5\.1'
        $text | Should -Not -Match '\?\?'
        $text | Should -Not -Match '-Parallel'
        $text | Should -Not -Match 'Join-String'
        $text | Should -Not -Match '\$PSStyle'
    }
}
