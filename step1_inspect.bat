@echo off
chcp 65001 >nul
echo Running Step 1 Inspection...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\step1_inspect.ps1"
echo.
echo Done! You can close this window and tell the assistant in chat.
pause
