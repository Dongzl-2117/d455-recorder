@echo off
rem D455 Studio: a web page for record_d455.py (live view, recording, settings, playback).
rem   studio.bat              start it and open http://127.0.0.1:8455/ in the browser
rem   studio.bat --simulate   try it without a camera
rem   studio.bat --doctor     check this computer and the camera connection, explain problems
setlocal
cd /d "%~dp0"
call run.bat --version
if errorlevel 1 goto failed
.venv\Scripts\python d455_studio.py %*
if errorlevel 1 goto failed
exit /b 0

:failed
rem Started by a double click the window would close at once; keep it open so the reason can be read.
echo.
echo D455 Studio stopped with an error. The lines above say why.
pause
exit /b 1
