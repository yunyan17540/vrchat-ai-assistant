@echo off
cd /d "%~dp0"
set "PYTHON=E:\prod tool\python.exe"
set "ROOT=%~dp0"
if exist "%ROOT%.steamvr_overlay.pid" (
  echo stop>"%ROOT%.steamvr_overlay.stop"
  for /l %%N in (1,1,15) do (
    if not exist "%ROOT%.steamvr_overlay.pid" goto start_app
    ping -n 2 127.0.0.1 >nul
  )
)
if exist "%ROOT%.steamvr_overlay.pid" (
  for /f "usebackq delims=" %%P in ("%ROOT%.steamvr_overlay.pid") do taskkill /PID %%P /F >nul 2>nul
  del /q "%ROOT%.steamvr_overlay.pid" 2>nul
)
:start_app
del /q "%ROOT%.steamvr_overlay.stop" 2>nul
start "VRChat AI Assistant" /b "%PYTHON%" "%ROOT%steamvr_overlay.py"
exit /b
