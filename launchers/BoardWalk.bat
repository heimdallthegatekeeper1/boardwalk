@echo off
setlocal
cd /d "%~dp0.."
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 launchers\boardwalk_launcher.py
) else (
  python launchers\boardwalk_launcher.py
)
if errorlevel 1 pause
endlocal
