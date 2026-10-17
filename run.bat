@echo off
rem ===========================================================================
rem  Bilibili user-comment crawler - one-click launcher
rem  Usage: double-click this file, then type a nickname when prompted.
rem  NOTE: kept pure ASCII; Chinese text in .bat can be mangled by the
rem        console code page and break the batch parser.
rem ===========================================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo         Install Python 3 from https://www.python.org/downloads/
    echo         and make sure "Add python.exe to PATH" is checked.
    echo.
    pause
    exit /b 1
)

python bili_comments.py %*

echo.
echo ------------------------------------------------------------
echo Done. Output files are saved in the "output" folder.
echo ------------------------------------------------------------
echo.
pause
endlocal
