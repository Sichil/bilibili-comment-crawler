@echo off
rem ===========================================================================
rem  Bilibili comment crawler - GUI launcher
rem  Double-click this file to open the graphical interface.
rem  NOTE: kept pure ASCII to avoid code page parsing issues.
rem ===========================================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo         Install Python 3 from https://www.python.org/downloads/
    echo         and check "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

python gui.py %*

if errorlevel 1 (
    echo.
    echo [ERROR] The GUI failed to start. See the message above.
    echo.
    pause
)
endlocal
