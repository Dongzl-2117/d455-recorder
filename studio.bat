@echo off
rem D455 Studio: a web page for record_d455.py (live view, recording, settings, playback).
rem   studio.bat              start it and open http://127.0.0.1:8455/ in the browser
rem   studio.bat --simulate   try it without a camera
setlocal
cd /d "%~dp0"
call run.bat --version
if errorlevel 1 exit /b 1
.venv\Scripts\python d455_studio.py %*
