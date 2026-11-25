$ErrorActionPreference = 'Stop'
# 由 desktop/app.py 的 _launch_installer 启动：宿主（桌面壳）已经排空并实退之后
# 才会走到这里。环境变量：
#   WB_UPDATE_SOURCE 已校验过的更新包（.exe）
#   WB_UPDATE_TARGET 要替换的当前 EXE
#   WB_UPDATE_PID    持有 EXE 句柄的 bootloader PID（PyInstaller onefile 的父进程）
#   WB_UPDATE_LOG    日志文件
$source = $env:WB_UPDATE_SOURCE
$target = $env:WB_UPDATE_TARGET
$backup = "$target.previous"
$temporary = "$target.new"
$taskLog = $env:WB_UPDATE_LOG
$replaced = $false
$restored = $false

function Write-UpdateLog([string]$text) {
    if ([string]::IsNullOrEmpty($taskLog)) { return }
    try {
        Add-Content -LiteralPath $taskLog -Value "$(Get-Date -Format o) $text"
    } catch { }
}

try {
    if ([string]::IsNullOrEmpty($source) -or -not (Test-Path -LiteralPath $source)) {
        throw "更新包不存在，已保留当前版本。"
    }
    if ([string]::IsNullOrEmpty($target)) {
        throw "缺少更新目标路径。"
    }
    # 1) 等宿主真的退出：bootloader 关掉之后 EXE 文件句柄才释放。
    #    正常路径由桌面控制器保证"排空 -> 实退 -> 启动安装器"，这里只是等句柄。
    $hostPid = 0
    if (-not [int]::TryParse([string]$env:WB_UPDATE_PID, [ref]$hostPid)) { $hostPid = 0 }
    if ($hostPid -gt 0) {
        Write-UpdateLog "等待宿主退出：PID $hostPid"
        $process = Get-Process -Id $hostPid -ErrorAction SilentlyContinue
        if ($process) { $process.WaitForExit() }
        Write-UpdateLog "宿主已退出：PID $hostPid"
    }
    # 2) 先复制到 .new：绝不直接删掉正在用的原文件。
    Copy-Item -LiteralPath $source -Destination $temporary -Force
    # 3) 备份当前版本为 .previous，再替换；失败按 500ms 重试（杀软/索引器会短暂占用）。
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            if (Test-Path -LiteralPath $target) {
                Copy-Item -LiteralPath $target -Destination $backup -Force
            }
            Move-Item -LiteralPath $temporary -Destination $target -Force
            $replaced = $true
            break
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $replaced) { throw "无法替换 EXE，已保留当前版本。" }
    # 4) 新 onefile 必须解压自己的运行目录，不能复用旧 bundle。
    $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
    Start-Process -FilePath $target -WorkingDirectory (Split-Path -Parent $target) -WindowStyle Hidden
    Write-UpdateLog "更新成功，旧版备份：$backup"
    exit 0
} catch {
    Write-UpdateLog "更新失败：$_"
    # 5) 复用 .previous 恢复：只要替换过就把旧版放回去，保证用户还能启动。
    if ($replaced -and (Test-Path -LiteralPath $backup)) {
        try {
            Copy-Item -LiteralPath $backup -Destination $target -Force
            $restored = $true
        } catch {
            Write-UpdateLog "恢复旧版失败：$_"
        }
    }
    $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
    if (Test-Path -LiteralPath $target) {
        try {
            Start-Process -FilePath $target -WindowStyle Hidden
            Write-UpdateLog "已启动现有版本（恢复=$restored）"
        } catch {
            Write-UpdateLog "启动现有版本失败：$_"
        }
    }
    exit 1
}
