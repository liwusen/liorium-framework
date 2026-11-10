<#
.SYNOPSIS
    把本地工作区的 12 个 liorium 包以可编辑模式装进指定解释器。

.DESCRIPTION
    与从 PyPI 安装走同一条路径（不额外注入 sys.path、不改宿主的环境约定）：
    `uv pip install -e packages/<pkg>`，版本解析交给 uv，和发布出去的包一致。

.EXAMPLE
    ./scripts/dev-install-liorium.ps1
    ./scripts/dev-install-liorium.ps1 -Python .venv\Scripts\python.exe -AllExtras
#>
param(
    [string]$Python = ".venv\Scripts\python.exe",
    [switch]$AllExtras
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

if (-not (Test-Path $Python)) {
    throw "找不到 Python 解释器: $Python"
}

$packages = @(
    "liorium-core",
    "liorium-vfs",
    "liorium-realm-base",
    "liorium-core-tool",
    "liorium-core-tool-realm",
    "liorium-memory",
    "liorium-skills",
    "liorium-mcp",
    "liorium-todo-realm",
    "liorium-provider-manager",
    "liorium-agent",
    "liorium-cli"
)

$targets = @()
foreach ($p in $packages) {
    $targets += (Join-Path $root "packages\$p")
}

if ($AllExtras) {
    $targets += @("langchain-openai", "langchain-anthropic", "langchain-google-genai")
}

Write-Host "uv pip install --python $Python (editable: $($packages -join ', '))"
uv pip install --python $Python --editable @targets
if ($LASTEXITCODE -ne 0) { throw "uv pip install 失败 (exit $LASTEXITCODE)" }
Write-Host "完成。"
