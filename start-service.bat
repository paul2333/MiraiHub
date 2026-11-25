@echo off
title workbuddy2api - Start
cd /d "%~dp0"
setlocal

echo ========================================
echo   workbuddy2api Start Service
echo ========================================
echo.

REM 先停掉旧实例 / 旧守护
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\daemon.ps1" -Stop >nul 2>&1

REM -Detach：用 Start-Process -WindowStyle Hidden 把守护进程完全脱离本窗口/会话。
REM 不要用 start /B：那会把服务挂在当前控制台上，关窗口即被回收。
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\daemon.ps1" -Detach

echo Starting detached daemon + service, please wait...
REM 冷启动要建表 / 预热 SSL / 探客户端目录，实测 35~50s 才监听。
REM 原来只等 6+5 秒，会把「还在启动」误报成「Start FAILED」。
set /a WAIT_TRIES=0
:waitloop
timeout /t 3 /nobreak >nul
netstat -ano | findstr ":8790" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 goto :started
set /a WAIT_TRIES+=1
if %WAIT_TRIES% lss 25 goto :waitloop
echo Start FAILED - check logs\daemon.log and logs\service.err.log
goto :after
:started
echo Service started OK!
:after

echo.
echo ========================================
echo   Admin UI : http://127.0.0.1:8790/admin
echo   Username : admin
echo   Password : Wb2api!Test2026
echo ========================================
echo.
echo Service runs detached. You can close this window now.
echo Watchdog auto-restarts the service if it dies.
echo To stop: double-click stop-service.bat
echo.
pause
