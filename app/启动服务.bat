@echo off
setlocal
rem Paper Book Companion - story service launcher.
rem This file is ASCII-only on purpose: cmd.exe codepage differences
rem (GBK/UTF-8) must not break batch parsing or garble messages.
pushd "%~dp0" || exit /b 1

set "PAPERBOOK_PYTHON=..\.venv\Scripts\python.exe"
if not exist "%PAPERBOOK_PYTHON%" (
    where python >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Python not found. Create the project .venv or add Python to PATH.
        popd
        pause
        exit /b 1
    )
    set "PAPERBOOK_PYTHON=python"
)

echo Starting story service...
echo The local /quick URL will open in your browser when startup completes.
echo Close this window or press Ctrl+C to stop the service.
echo.

"%PAPERBOOK_PYTHON%" -u story_tts_server.py --open-quick
set "PAPERBOOK_EXIT_CODE=%errorlevel%"
popd

echo.
echo Service exited with code %PAPERBOOK_EXIT_CODE%
if not "%PAPERBOOK_EXIT_CODE%"=="0" (
    echo [ERROR] Startup or runtime failed. See the messages above.
)
pause
exit /b %PAPERBOOK_EXIT_CODE%
