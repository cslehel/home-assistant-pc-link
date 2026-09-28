@echo off
rem Double-click to install or upgrade Home Assistant PC Link (asks for administrator rights).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
