<#
.SYNOPSIS
    本地跑测试。

.DESCRIPTION
    优先级：环境变量 LIORIUM_TEST_PYTHON → 仓库根的 .venv（uv 装的正式环境，与 CI 一致）。

    注意：这里**不能**声明 `$Python` / `$PytestArgs` 之类的具名参数 —— PowerShell
    会把 pytest 的 `-p` / `-q` 当成参数名前缀去匹配（`-p` 命中 `-PytestArgs`），
    导致参数被吞。所以只接收位置参数到 `$args`。

.EXAMPLE
    ./scripts/test.ps1
    ./scripts/test.ps1 -q packages/liorium-core
    ./scripts/test.ps1 -m live
    $env:LIORIUM_TEST_PYTHON = ".venv\Scripts\python.exe"; ./scripts/test.ps1
#>

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$candidates = @()
if ($env:LIORIUM_TEST_PYTHON) { $candidates += $env:LIORIUM_TEST_PYTHON }
$candidates += (Join-Path $root ".venv\Scripts\python.exe")
$candidates += ".venv\Scripts\python.exe"

$python = $null
foreach ($candidate in $candidates) {
    if ($candidate -and (Test-Path $candidate)) { $python = $candidate; break }
}
if (-not $python) {
    throw "找不到 Python 解释器；先跑 uv sync 或设置 LIORIUM_TEST_PYTHON"
}

Write-Host "pytest @ $python"
Push-Location $root
try {
    & $python -m pytest @args
}
finally {
    Pop-Location
}
exit $LASTEXITCODE
