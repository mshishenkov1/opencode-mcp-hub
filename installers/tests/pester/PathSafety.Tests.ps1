#Requires -Version 5.1
#
# installers/tests/pester/PathSafety.Tests.ps1 — единая нормализация путей и строгая вложенность
# purge_paths на Windows-ветке (N5-P3, N5-R2). Паритет с path_normalize / path_is_inside /
# purge_path_is_safe из common/install-posix.sh; POSIX-сторона покрыта
# installers/tests/bats/37-path-normalization.bats.
#
# Модель угрозы (reports/review-i5-3.json, blocker): проверка «путь внутри профиля и не равен
# самому профилю» велась над СЫРОЙ строкой, а Remove-Item работал над строкой, которую файловая
# система схлопывает сама. Формы '%USERPROFILE%\', '%USERPROFILE%\\', '%USERPROFILE%\.',
# '%USERPROFILE%\.\', '%USERPROFILE%\sub\..' — это тот же каталог профиля, и каждая обязана
# отвергаться до Remove-Item.
#
# Платформа прогона. Задание `pester` в installers/ci/installers.yml запускает набор на pwsh под
# ubuntu-latest, где '\' — обычный символ имени файла, а не разделитель: схлопывание форм с
# обратным слэшем выполняет только Windows-реализация [IO.Path]::GetFullPath. Поэтому таблица
# форм строится на РОДНОМ разделителе текущей платформы (там проверка содержательна на обеих),
# а буквальные варианты с '\' вынесены в отдельные проверки, которые на не-Windows помечаются
# Skipped, а не проходят вхолостую. Проверки, не зависящие от разделителя (сегмент '..',
# относительный путь, путь вне профиля), выполняются везде.
#
# Файл к реестру и переменным окружения машины не обращается: граница среды — единственный мок
# Get-UserProfileDir (см. тест AC-126 в Uninstall.Tests.ps1).

BeforeAll {
    $script:InstallersRoot = (Resolve-Path (Join-Path $PSScriptRoot '..' | Join-Path -ChildPath '..')).Path
    $script:InstallScript = Join-Path (Join-Path $script:InstallersRoot 'windows') 'install.ps1'

    # Dot-source: исполняемый вход не срабатывает (N5-T2).
    . $script:InstallScript

    function New-TempDir {
        $path = Join-Path ([IO.Path]::GetTempPath()) ('opencode-pester-' + [Guid]::NewGuid().ToString('N'))
        $null = New-Item -ItemType Directory -Path $path -Force
        return $path
    }

    # Разделитель, который на текущей платформе действительно схлопывает GetFullPath.
    function Get-NativeSeparator {
        return [string][System.IO.Path]::DirectorySeparatorChar
    }

    # Платформа прогона, а не платформа установщика: Test-IsWindowsHost подменяется моками и для
    # этого решения не годится.
    function Test-NativeWindowsHost {
        return ([System.IO.Path]::DirectorySeparatorChar -eq '\')
    }

    # Тот же путь, которым идёт Confirm-PurgePathList: раскрытие шаблона манифеста, затем проверка.
    function Test-RawPurgePath {
        param([string]$Raw)
        $expanded = Expand-UserPathTemplate -Value $Raw
        return (Test-PurgePathSafe -Raw $Raw -Expanded $expanded)
    }
}

Describe 'Единая нормализация путей ConvertTo-NormalPath (N5-P3)' -Tag 'ci' {
    BeforeEach {
        $script:HomeRoot = New-TempDir
        $script:Sep = Get-NativeSeparator
    }

    AfterEach {
        Remove-Item -LiteralPath $script:HomeRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    It 'AC-135, AC-143: формы, схлопывающиеся в сам каталог, дают одно и то же значение' {
        $expected = ConvertTo-NormalPath $script:HomeRoot
        $expected | Should -Not -BeNullOrEmpty
        $forms = @(
            ($script:HomeRoot + $script:Sep),
            ($script:HomeRoot + $script:Sep + $script:Sep),
            ($script:HomeRoot + $script:Sep + $script:Sep + $script:Sep),
            ($script:HomeRoot + $script:Sep + '.'),
            ($script:HomeRoot + $script:Sep + '.' + $script:Sep),
            ($script:HomeRoot + $script:Sep + '.' + $script:Sep + '.')
        )
        foreach ($form in $forms) {
            ConvertTo-NormalPath $form | Should -Be $expected -Because "форма '$form' — тот же каталог"
        }
    }

    It 'AC-143: сегмент ".." отвергается — лексический подъём не выполняется' {
        ConvertTo-NormalPath ($script:HomeRoot + $script:Sep + 'sub' + $script:Sep + '..') | Should -BeNullOrEmpty
        ConvertTo-NormalPath ($script:HomeRoot + $script:Sep + '..') | Should -BeNullOrEmpty
        ConvertTo-NormalPath ($script:HomeRoot + $script:Sep + '..' + $script:Sep + 'other') | Should -BeNullOrEmpty
        ConvertTo-NormalPath '..' | Should -BeNullOrEmpty
    }

    It 'AC-143: относительный и пустой путь отвергаются — результат всегда абсолютный' {
        ConvertTo-NormalPath ('opencode' + $script:Sep + 'data') | Should -BeNullOrEmpty
        ConvertTo-NormalPath 'opencode' | Should -BeNullOrEmpty
        ConvertTo-NormalPath '' | Should -BeNullOrEmpty
        ConvertTo-NormalPath '   ' | Should -BeNullOrEmpty
        ConvertTo-NormalPath $null | Should -BeNullOrEmpty
    }

    It 'AC-143: вложенный путь нормализуется, но остаётся собой' {
        $want = ConvertTo-NormalPath (Join-Path (Join-Path $script:HomeRoot '.config') 'opencode')
        $want | Should -Not -BeNullOrEmpty
        $noisy = $script:HomeRoot + $script:Sep + $script:Sep + '.config' + $script:Sep + '.' + $script:Sep + 'opencode' + $script:Sep
        ConvertTo-NormalPath $noisy | Should -Be $want
    }

    It 'AC-143: формы профиля с обратным слэшем схлопываются (Windows)' {
        if (-not (Test-NativeWindowsHost)) {
            Set-ItResult -Skipped -Because 'обратный слэш схлопывает только Windows-реализация GetFullPath: на pwsh под Linux "\" — обычный символ имени файла'
            return
        }
        $expected = ConvertTo-NormalPath $script:HomeRoot
        foreach ($form in @("$script:HomeRoot\", "$script:HomeRoot\\", "$script:HomeRoot\.", "$script:HomeRoot\.\")) {
            ConvertTo-NormalPath $form | Should -Be $expected -Because "форма '$form' — тот же каталог"
        }
        ConvertTo-NormalPath "$script:HomeRoot\sub\.." | Should -BeNullOrEmpty
    }
}

Describe 'Строгая вложенность Test-PathInside (N5-R2)' -Tag 'ci' {
    BeforeEach {
        $script:HomeRoot = New-TempDir
        $script:Sep = Get-NativeSeparator
    }

    AfterEach {
        Remove-Item -LiteralPath $script:HomeRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    It 'AC-143: объект ниже каталога — истина' {
        $parent = ConvertTo-NormalPath $script:HomeRoot
        $child = ConvertTo-NormalPath (Join-Path (Join-Path $script:HomeRoot '.config') 'opencode')
        Test-PathInside -Parent $parent -Child $child | Should -BeTrue
    }

    It 'AC-143: сам каталог, его родитель и сосед с общим префиксом — ложь' {
        $parent = ConvertTo-NormalPath $script:HomeRoot
        Test-PathInside -Parent $parent -Child $parent | Should -BeFalse
        Test-PathInside -Parent $parent -Child ($parent + $script:Sep) | Should -BeFalse
        Test-PathInside -Parent $parent -Child ($parent + '-neighbour') | Should -BeFalse
        Test-PathInside -Parent $parent -Child (Split-Path -Path $parent -Parent) | Should -BeFalse
    }

    It 'AC-143: пустые аргументы — ложь' {
        $parent = ConvertTo-NormalPath $script:HomeRoot
        Test-PathInside -Parent $parent -Child '' | Should -BeFalse
        Test-PathInside -Parent '' -Child $parent | Should -BeFalse
    }
}

Describe 'Безопасность purge_paths Test-PurgePathSafe (N5-R2)' -Tag 'ci' {
    BeforeEach {
        $script:HomeRoot = New-TempDir
        $script:Sep = Get-NativeSeparator
        Mock Get-UserProfileDir { return $script:HomeRoot }
        Mock Get-LocalAppDataDir { return (Join-Path $script:HomeRoot 'AppData') }
    }

    AfterEach {
        Remove-Item -LiteralPath $script:HomeRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    It 'AC-91, AC-143: сам профиль во всех схлопывающихся формах отвергается' {
        $forms = @(
            '%USERPROFILE%',
            ('%USERPROFILE%' + $script:Sep),
            ('%USERPROFILE%' + $script:Sep + $script:Sep),
            ('%USERPROFILE%' + $script:Sep + $script:Sep + $script:Sep),
            ('%USERPROFILE%' + $script:Sep + '.'),
            ('%USERPROFILE%' + $script:Sep + '.' + $script:Sep),
            ('%USERPROFILE%' + $script:Sep + 'sub' + $script:Sep + '..'),
            '~',
            '${HOME}'
        )
        foreach ($form in $forms) {
            Test-RawPurgePath $form | Should -BeFalse -Because "форма '$form' ссылается на сам профиль"
        }
    }

    It 'AC-91, AC-143: буквальные формы с обратным слэшем отвергаются (Windows)' {
        if (-not (Test-NativeWindowsHost)) {
            Set-ItResult -Skipped -Because 'обратный слэш схлопывает только Windows-реализация GetFullPath: на pwsh под Linux "\" — обычный символ имени файла'
            return
        }
        foreach ($form in @('%USERPROFILE%\', '%USERPROFILE%\\', '%USERPROFILE%\.', '%USERPROFILE%\.\', '%USERPROFILE%\sub\..')) {
            Test-RawPurgePath $form | Should -BeFalse -Because "форма '$form' ссылается на сам профиль"
        }
    }

    It 'AC-90, AC-143: путь вне профиля отвергается' {
        Test-RawPurgePath 'C:\Windows' | Should -BeFalse
        Test-RawPurgePath 'C:\' | Should -BeFalse
        Test-RawPurgePath '/etc' | Should -BeFalse
        # Сосед с общим префиксом: строковое сравнение без проверки границы сегмента засчитало бы
        # его как «внутри профиля».
        Test-PurgePathSafe -Raw 'сосед' -Expanded ($script:HomeRoot + '-neighbour') | Should -BeFalse
    }

    It 'AC-90, AC-143: относительный путь и пустое значение отвергаются' {
        Test-RawPurgePath ('opencode' + $script:Sep + 'data') | Should -BeFalse
        Test-RawPurgePath 'opencode' | Should -BeFalse
        Test-PurgePathSafe -Raw '' -Expanded $script:HomeRoot | Should -BeFalse
        Test-PurgePathSafe -Raw 'x' -Expanded '' | Should -BeFalse
    }

    It 'AC-90, AC-143: ".." отвергается и по сырому значению, и по раскрытому' {
        Test-RawPurgePath ('~' + $script:Sep + '..' + $script:Sep + 'other') | Should -BeFalse
        Test-PurgePathSafe -Raw 'x' -Expanded ($script:HomeRoot + $script:Sep + '..' + $script:Sep + 'other') | Should -BeFalse
    }

    It 'AC-89, AC-143: штатные пути внутри профиля по-прежнему проходят' {
        Test-RawPurgePath ('%USERPROFILE%' + $script:Sep + '.config' + $script:Sep + 'opencode') | Should -BeTrue
        Test-RawPurgePath '${XDG_CONFIG_HOME}/opencode' | Should -BeTrue
        Test-RawPurgePath '${XDG_DATA_HOME}/opencode' | Should -BeTrue
        Test-RawPurgePath ('~' + $script:Sep + '.config' + $script:Sep + 'opencode') | Should -BeTrue
    }

    It 'AC-89, AC-143: план удаления показывает нормализованный путь, а не исходную строку' {
        $noisy = '%USERPROFILE%' + $script:Sep + $script:Sep + '.config' + $script:Sep + '.' + $script:Sep + 'opencode'
        $expanded = Expand-UserPathTemplate -Value $noisy
        $normalized = ConvertTo-NormalPath $expanded
        $normalized | Should -Be (ConvertTo-NormalPath (Join-Path (Join-Path $script:HomeRoot '.config') 'opencode'))
        Test-PurgePathSafe -Raw $noisy -Expanded $expanded | Should -BeTrue
    }
}
