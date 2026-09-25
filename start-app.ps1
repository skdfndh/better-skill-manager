$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $scriptDir

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    throw "未找到 Python。请安装 Python 3.10 或更新版本，或直接运行 dist\SkillShelf.exe。"
}

$venvPath = Join-Path $scriptDir ".venv-desktop"
$venvPython = Join-Path $venvPath "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    & python -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw "创建桌面运行环境失败。" }
}
& $venvPython -c "import webview" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $venvPython -m pip install -r requirements-app.txt
    if ($LASTEXITCODE -ne 0) { throw "桌面版依赖安装失败。" }
}
& $venvPython desktop_app.py
if ($LASTEXITCODE -ne 0) {
    throw "Skill Shelf 桌面版启动失败。"
}
