@echo off
rem Double-click: installs Python + QEMU if missing, then opens the GUI.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_and_run.ps1"
if errorlevel 1 pause
