@echo off
rem Paper Book Companion - story service launcher.
rem This file is ASCII-only on purpose: cmd.exe codepage differences
rem (GBK/UTF-8) must not break batch parsing or garble messages.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python and add it to PATH.
    echo Check with: where python
    pause
    exit /b 1
)

echo Starting story service...
echo URL: http://localhost:5000/tts_v2.html
echo Close this window or press Ctrl+C to stop the service.
echo.

python -u story_tts_server.py
set "EXITCODE=%errorlevel%"

echo.
echo Service exited with code %EXITCODE%
if not "%EXITCODE%"=="0" (
    echo [ERROR] Startup or runtime failed. See the messages above.
)
pause
