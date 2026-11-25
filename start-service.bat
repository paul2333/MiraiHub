@echo off
setlocal
title workbuddy2api - Start
cd /d "%~dp0"

echo ========================================
echo   workbuddy2api Start Service
echo ========================================
echo.

REM 先停**本部署**的旧守护（不会枚举、也不会杀别的 python 进程）。
set "PORTARG="
if not "%~1"=="" set "PORTARG=-Port %~1"
if "%PORTARG%"=="" if not "%WB_PORT%"=="" set "PORTARG=-Port %WB_PORT%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\daemon.ps1" -Stop %PORTARG% >nul 2>&1

REM -Detach：用 Start-Process -WindowStyle Hidden 把守护进程完全脱离本窗口/会话。
REM 不要用 start /B：那会把服务挂在当前控制台上，关窗口即被回收。
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\daemon.ps1" -Detach %PORTARG%

REM 探活不看端口：一律问所有者状态（python main.py --status 的真实结果）。
REM ready 才算起来；starting 是慢启动，继续等；draining/stopping 是主动切换。
set "TRIES=0"
:waitready
set /a TRIES+=1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\daemon.ps1" -Status %PORTARG% >nul 2>&1
if not errorlevel 1 goto :started
if %TRIES% GEQ 20 goto :failed
timeout /t 3 /nobreak >nul
goto :waitready

:started
echo Service started OK!
set "RC=0"
goto :report

:failed
echo Start did not become ready. Check logs\daemon.log and logs\admin.log
set "RC=1"
goto :report

:report
echo.
echo ========================================
echo   Admin UI : http://127.0.0.1:8790/admin
echo ========================================
echo.
echo Service runs detached (watchdog restarts it only when the owner is really gone).
echo To stop: double-click stop-service.bat
echo.
pause
exit /b %RC%
