@echo off
setlocal EnableDelayedExpansion

rem Llama.cpp Prompt-Cache Session Manager - Windows launcher
rem -----------------------------------------------------------------
rem Double-click this file in Explorer (or run it from a command
rem prompt) to start the manager. This file is only a thin,
rem OS-specific wrapper: all real program logic lives in
rem llama_cache_manager.py, so behavior is identical to the macOS
rem .command launcher and any other future front end.
rem
rem Both this file AND llama_cache_manager.py must be in the same
rem folder - this launcher does nothing on its own.

rem Always run from the directory this script lives in, so config.json
rem and the default "llama_sessions" cache folder resolve next to it,
rem no matter how the file was launched (double-click, shortcut, etc).
cd /d "%~dp0"

if not exist "llama_cache_manager.py" (
    echo ============================================================
    echo  llama_cache_manager.py was not found in this folder:
    echo    %cd%
    echo.
    echo  Put this .bat file in the same folder as
    echo  llama_cache_manager.py and try again.
    echo ============================================================
    echo.
    pause
    exit /b 1
)

rem --- Locate a Python 3 interpreter -------------------------------------
set "PY_CMD="

where py >nul 2>nul
if not errorlevel 1 (
    set "PY_CMD=py -3"
)

if not defined PY_CMD (
    where python >nul 2>nul
    if not errorlevel 1 (
        for /f "delims=" %%V in ('python -c "import sys; print(sys.version_info[0])" 2^>nul') do set "PYVER=%%V"
        if "!PYVER!"=="3" (
            set "PY_CMD=python"
        )
    )
)

if not defined PY_CMD (
    where python3 >nul 2>nul
    if not errorlevel 1 (
        set "PY_CMD=python3"
    )
)

if not defined PY_CMD (
    echo ============================================================
    echo  Python 3 was not found on this computer.
    echo.
    echo  Install it from: https://www.python.org/downloads/
    echo  During setup, check the box "Add python.exe to PATH".
    echo ============================================================
    echo.
    pause
    exit /b 1
)

rem --- Run the cross-platform program ------------------------------------
%PY_CMD% "llama_cache_manager.py" %*
set "STATUS=%ERRORLEVEL%"

echo.
if not "%STATUS%"=="0" (
    echo The program exited with an error ^(code %STATUS%^).
)
pause
exit /b %STATUS%
