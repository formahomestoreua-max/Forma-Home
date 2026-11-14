@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title Forma Home v28 - Original Photos
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
if errorlevel 1 (
  echo ERROR: Cannot open the project directory.
  echo Extract the ZIP first, for example to Desktop\Forma_Home.
  goto :failed
)
if not exist "server.py" (
  echo ERROR: server.py was not found in this directory.
  echo Extract the entire ZIP and run this .bat inside Forma_Home.
  goto :failed
)
echo.
echo ===========================================================
echo  FORMA HOME - LOCAL STORE
 echo ===========================================================
echo.
set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=py -3"
)
if not defined PYTHON_CMD (
  where python >nul 2>nul
  if not errorlevel 1 (
    python -c "import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python"
  )
)
if not defined PYTHON_CMD (
  where python3 >nul 2>nul
  if not errorlevel 1 (
    python3 -c "import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python3"
  )
)
if not defined PYTHON_CMD (
  echo ERROR: Python 3.10 or newer is not installed or not in PATH.
  echo 1. Install it from https://www.python.org/downloads/windows/
  echo 2. In the installer, select "Add python.exe to PATH".
  echo 3. Close this window and double-click start_windows.bat again.
  echo This website needs Python for accounts, orders and admin panel.
  goto :failed
)
echo Python: %PYTHON_CMD%
echo Store: http://127.0.0.1:8768/ (Forma Home v28)
echo Admin: http://127.0.0.1:8768/admin
echo.
echo At first launch, choose an admin password of at least 12 characters.
echo Password input will remain invisible while typing. This is normal.
echo Leave this window open while you use the website.
echo.
set "FORMA_PORT=8768"
%PYTHON_CMD% -u "server.py"
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="0" goto :done
echo.
echo ERROR: The server exited with code %EXIT_CODE%.
echo Read the message above. Common reasons:
echo - Port 8768 is already used by another Forma Home v27 window.
echo - Python installation is incomplete.
echo - OneDrive blocks editing files in this folder.
echo Try moving the extracted folder to Desktop\Forma_Home.
goto :failed
:done
echo.
echo Server stopped normally.
pause
exit /b 0
:failed
echo.
echo The window will stay open so you can read the error.
pause
exit /b 1
