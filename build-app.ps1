$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $scriptDir

$venvPath = Join-Path $scriptDir ".venv-desktop"
$venvPython = Join-Path $venvPath "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    & python -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw "创建桌面构建环境失败。" }
}
& $venvPython -c "import PyInstaller, webview" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $venvPython -m pip install -r requirements-desktop.txt
    if ($LASTEXITCODE -ne 0) { throw "桌面版构建依赖安装失败。" }
}
& $venvPython -m PyInstaller --noconfirm --clean --onefile --windowed --name SkillShelf --add-data "$scriptDir\index.html;." --add-data "$scriptDir\roots.json;." --hidden-import webview.platforms.winforms --hidden-import webview.platforms.edgechromium --hidden-import clr_loader --hidden-import pythonnet desktop_app.py
if ($LASTEXITCODE -ne 0) { throw "桌面应用打包失败。" }
$exePath = Join-Path (Join-Path $scriptDir "dist") "SkillShelf.exe"
if (-not (Test-Path -LiteralPath $exePath)) { throw "桌面应用生成后未找到：$exePath" }
Write-Host "桌面应用已生成：$exePath"

$desktopPath = [Environment]::GetFolderPath("Desktop")
if (-not $desktopPath) { throw "无法确定当前用户的桌面目录。" }
$shell = New-Object -ComObject WScript.Shell
$shortcutPath = $null
$projectPrefix = $scriptDir.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
foreach ($candidate in Get-ChildItem -LiteralPath $desktopPath -Filter "SkillShelf*.lnk" -File -ErrorAction SilentlyContinue) {
    $existingShortcut = $shell.CreateShortcut($candidate.FullName)
    $targetPath = $existingShortcut.TargetPath
    if ($targetPath) {
        $normalizedTarget = [System.IO.Path]::GetFullPath($targetPath)
        $belongsToProject = $normalizedTarget.StartsWith($projectPrefix, [System.StringComparison]::OrdinalIgnoreCase)
        $isSkillShelf = [System.IO.Path]::GetFileName($normalizedTarget).Equals("SkillShelf.exe", [System.StringComparison]::OrdinalIgnoreCase)
        if ($belongsToProject -and $isSkillShelf) {
            $shortcutPath = $candidate.FullName
            break
        }
    }
}
if (-not $shortcutPath) {
    $shortcutPath = Join-Path $desktopPath "SkillShelf.lnk"
    $suffix = 2
    while (Test-Path -LiteralPath $shortcutPath) {
        $shortcutPath = Join-Path $desktopPath "SkillShelf ($suffix).lnk"
        $suffix++
    }
}
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $exePath
$shortcut.WorkingDirectory = Split-Path -Parent $exePath
$shortcut.IconLocation = "$exePath,0"
$shortcut.Description = "SkillShelf desktop app"
$shortcut.Save()
Write-Host "桌面快捷方式已创建或更新：$shortcutPath"
