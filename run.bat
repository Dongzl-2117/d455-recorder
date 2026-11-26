@echo off
rem One-step launcher for record_d455.py on Windows.
rem   run.bat                 record until Ctrl+C
rem   run.bat --duration 600  any record_d455.py argument is passed through
rem   run.bat selftest        check the installation without a camera
setlocal
cd /d "%~dp0"

if not exist ".venv\.installed" (
  echo First run: creating .venv and installing packages, one time only...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if errorlevel 1 (
    echo Python 3.9 or newer was not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    exit /b 1
  )
  .venv\Scripts\python -m pip install --quiet --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Package installation failed. Check the network connection and run this file again.
    exit /b 1
  )
  type nul > ".venv\.installed"
)

.venv\Scripts\python record_d455.py %*
