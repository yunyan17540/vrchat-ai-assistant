@echo off
cd /d "%~dp0"
call "%~dp0start_steamvr_ai.bat"
start "VRChat AI Settings" /b "E:\prod tool\python.exe" "%~dp0steamvr_settings.py"
