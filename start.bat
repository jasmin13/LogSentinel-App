@echo off
cd /d "%~dp0"
title LogSentinel Studio - Enterprise On-Premises Log and Incident Diagnostics
echo =======================================================
echo         LogSentinel Studio - Enterprise AI Engine
echo      100%% Offline / Zero Cloud Leaks / Local CPU
echo =======================================================
echo.
echo Starting Local AI Server on Port 8002...
echo Browser will automatically open once the server is ready...
echo.
set PYTHONUNBUFFERED=1
start /b python open_browser.py
python -m uvicorn server:app --host 127.0.0.1 --port 8002
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Server stopped with error code %ERRORLEVEL%.
)
pause
