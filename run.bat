@echo off
rem One-step launcher for record_d455.py on Windows.
rem   run.bat                 record until Ctrl+C
rem   run.bat --duration 600  any record_d455.py argument is passed through
rem   run.bat selftest        check the installation without a camera
rem First run: creates .venv next to this file and installs the packages (needs internet once).
rem Python 3.12 is preferred when several are installed: the newest Python often has no RealSense package yet.
setlocal
cd /d "%~dp0"
if exist ".venv\.installed" goto run

echo First run: creating .venv and installing packages, one time only...
set "PYCMD="
py -3.12 -c "import sys" >nul 2>&1 && set "PYCMD=py -3.12"
if not defined PYCMD py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1 && set "PYCMD=py -3"
if not defined PYCMD python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1 && set "PYCMD=python"
if not defined PYCMD goto nopython
if exist ".venv" rmdir /s /q ".venv"
%PYCMD% -m venv .venv
if errorlevel 1 goto novenv
.venv\Scripts\python -m pip install --quiet --upgrade pip
if not exist logs mkdir logs
.venv\Scripts\python -m pip install -r requirements.txt > logs\pip_install.log 2>&1
if errorlevel 1 goto pipfailed
type nul > ".venv\.installed"
echo Packages installed.

:run
.venv\Scripts\python record_d455.py %*
exit /b %errorlevel%

:nopython
echo Python 3.9 or newer was not found. Install Python 3.12 from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" in the installer, then run this file again.
exit /b 1

:novenv
echo Could not create the .venv folder with %PYCMD%. Reinstall Python 3.12 from python.org and run this file again.
exit /b 1

:pipfailed
powershell -NoProfile -Command "Get-Content logs\pip_install.log -Tail 12" 2>nul
echo.
findstr /i /c:"No matching distribution found for pyrealsense2" /c:"satisfies the requirement pyrealsense2" logs\pip_install.log >nul
if not errorlevel 1 (
  echo Installation failed: RealSense publishes no pyrealsense2 package for this Python version. This is not a network problem.
  echo Install Python 3.12 from python.org, delete the .venv folder next to this file, and run this file again.
  exit /b 1
)
findstr /i /c:"Failed to establish" /c:"timed out" /c:"ProxyError" /c:"SSLError" /c:"getaddrinfo failed" /c:"CERTIFICATE_VERIFY_FAILED" logs\pip_install.log >nul
if not errorlevel 1 (
  echo Installation failed: the Python package index pypi.org cannot be reached. Check the network or proxy and run this file again.
  exit /b 1
)
findstr /i /c:"No space left" /c:"There is not enough space" logs\pip_install.log >nul
if not errorlevel 1 (
  echo Installation failed: the disk is full. Free a few GB and run this file again.
  exit /b 1
)
echo Installation failed. The full output is in logs\pip_install.log; send its last 30 lines to the maintainer.
exit /b 1
