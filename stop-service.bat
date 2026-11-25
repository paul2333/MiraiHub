@echo off
setlocal
title workbuddy2api - Stop
cd /d "%~dp0"

echo ========================================
echo   workbuddy2api Stop Service
echo ========================================
echo.

REM 停止 = 先停本部署的守护拉起，再由监督器正常排空、等它真的退出。
REM 这里刻意不做强杀兜底：强杀会截断在途请求，而且"谁在监听某个端口"
REM 从来不能证明进程归属。等价的手工命令：python main.py --stop --port PORT
set "PORTARG="
if not "%~1"=="" set "PORTARG=-Port %~1"
if "%PORTARG%"=="" if not "%WB_PORT%"=="" set "PORTARG=-Port %WB_PORT%"
set "WAITARG="
if not "%WB_STOP_WAIT%"=="" set "WAITARG=-WaitTimeout %WB_STOP_WAIT%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\daemon.ps1" -Stop %PORTARG% %WAITARG%
set "RC=%ERRORLEVEL%"

if "%RC%"=="0" (
    echo Service stopped.
) else (
    echo Stop did not finish: exit code %RC%
    echo   4 = still draining ^(no forced kill^), 5 = nothing running, 6 = status unavailable
    echo   See logs\daemon.log for details.
)
echo.
pause
exit /b %RC%
